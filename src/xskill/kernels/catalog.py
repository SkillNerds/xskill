"""Kernel discovery for bundled implementations and local bridge scripts."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Type

from xskill.kernels.base import BaseKernel, KernelManifest, validate_kernel_id
from xskill.kernels.builtin import BUILTIN_KERNELS


class KernelLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class KernelDescriptor:
    id: str
    name: str
    version: str
    description: str
    triggers: tuple[str, ...]
    source: str
    available: bool
    error: str
    plugin_path: Path | None
    config_path: Path | None
    workspace: Path

    def as_dict(self, *, active: bool = False) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "triggers": list(self.triggers),
            "source": self.source,
            "available": self.available,
            "error": self.error,
            "active": active,
            "plugin_path": str(self.plugin_path) if self.plugin_path else None,
            "config_path": str(self.config_path) if self.config_path else None,
            "workspace": str(self.workspace),
        }


class KernelCatalog:
    """Discover trusted ``<plugin_dir>/<id>/kernel.py`` bridge modules.

    A bridge exports ``KERNEL_CLASS`` whose class derives from ``BaseKernel``.
    The module is expected to import its own SDK package.  Import failures are
    preserved as unavailable catalog entries so the dashboard can explain how
    to repair an optional dependency.
    """

    def __init__(self, *, plugin_dir: Path, xskill_home: Path):
        self.plugin_dir = Path(plugin_dir).expanduser().resolve()
        self.xskill_home = Path(xskill_home).expanduser().resolve()
        self._classes: dict[str, Type[BaseKernel]] = dict(BUILTIN_KERNELS)
        self._descriptors: dict[str, KernelDescriptor] = {}
        self._discover()

    def list(self) -> list[KernelDescriptor]:
        return [self._descriptors[key] for key in sorted(self._descriptors)]

    def get(self, kernel_id: str) -> KernelDescriptor:
        normalized = validate_kernel_id(kernel_id)
        try:
            return self._descriptors[normalized]
        except KeyError as exc:
            raise KernelLoadError(f"kernel not found: {normalized}") from exc

    def create(self, kernel_id: str) -> BaseKernel:
        descriptor = self.get(kernel_id)
        if not descriptor.available:
            raise KernelLoadError(
                f"kernel {descriptor.id} is unavailable: {descriptor.error}"
            )
        kernel_class = self._classes.get(descriptor.id)
        if kernel_class is None:
            raise KernelLoadError(f"kernel class missing: {descriptor.id}")
        try:
            return kernel_class()
        except Exception as exc:
            raise KernelLoadError(
                f"kernel {descriptor.id} initialization failed: {exc}"
            ) from exc

    def _discover(self) -> None:
        for kernel_id, kernel_class in BUILTIN_KERNELS.items():
            self._descriptors[kernel_id] = self._descriptor_for(
                kernel_class.manifest,
                source="builtin",
                plugin_path=None,
            )
        if not self.plugin_dir.is_dir():
            return
        for child in sorted(self.plugin_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                directory_id = validate_kernel_id(child.name)
            except ValueError:
                continue
            bridge = child / "kernel.py"
            if not bridge.is_file() or directory_id in self._descriptors:
                continue
            try:
                kernel_class = self._load_class(bridge)
                manifest = kernel_class.manifest
                if manifest.id != directory_id:
                    raise KernelLoadError(
                        f"manifest id {manifest.id!r} must match directory "
                        f"{directory_id!r}"
                    )
                self._classes[directory_id] = kernel_class
                self._descriptors[directory_id] = self._descriptor_for(
                    manifest,
                    source="local-script",
                    plugin_path=bridge,
                )
            except Exception as exc:
                self._descriptors[directory_id] = KernelDescriptor(
                    id=directory_id,
                    name=directory_id,
                    version="unknown",
                    description="Local kernel bridge failed to import.",
                    triggers=(),
                    source="local-script",
                    available=False,
                    error=f"{type(exc).__name__}: {exc}",
                    plugin_path=bridge,
                    config_path=child / "config.yaml",
                    workspace=child / "workspace",
                )

    def _descriptor_for(
        self,
        manifest: KernelManifest,
        *,
        source: str,
        plugin_path: Path | None,
    ) -> KernelDescriptor:
        root = self.plugin_dir / manifest.id
        return KernelDescriptor(
            id=manifest.id,
            name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            triggers=tuple(manifest.triggers),
            source=source,
            available=True,
            error="",
            plugin_path=plugin_path,
            # Native uses XSkill's platform config; it has no private config.
            config_path=None if manifest.id == "native" else root / "config.yaml",
            workspace=root / "workspace",
        )

    @staticmethod
    def _load_class(bridge: Path) -> Type[BaseKernel]:
        fingerprint = hashlib.sha256(
            f"{bridge}:{bridge.stat().st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:16]
        module_name = f"_xskill_kernel_{fingerprint}"
        spec = importlib.util.spec_from_file_location(module_name, bridge)
        if spec is None or spec.loader is None:
            raise KernelLoadError(f"cannot create import spec for {bridge}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        kernel_class = getattr(module, "KERNEL_CLASS", None)
        if not isinstance(kernel_class, type) or not issubclass(
            kernel_class, BaseKernel
        ):
            raise KernelLoadError(
                f"{bridge} must export KERNEL_CLASS: type[BaseKernel]"
            )
        if not isinstance(getattr(kernel_class, "manifest", None), KernelManifest):
            raise KernelLoadError("KERNEL_CLASS.manifest must be KernelManifest")
        return kernel_class

