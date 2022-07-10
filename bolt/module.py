__all__ = [
    "Module",
    "CompiledModule",
    "ModuleManager",
    "ModuleCacheBackend",
    "CodegenResult",
    "UnusableCompilationUnit",
]


import logging
import marshal
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BufferedReader, BufferedWriter
from pathlib import Path
from types import CodeType
from typing import Any, Callable, Dict, List, Optional, Set, Union

from beet import TextFile, TextFileBase
from beet.core.utils import JsonDict, extra_field, import_from_string, required_field
from mecha import (
    AstCacheBackend,
    AstRoot,
    CompilationDatabase,
    CompilationUnit,
    Dispatcher,
    MechaError,
)

from .utils import rewrite_traceback

logger = logging.getLogger("mecha")


class UnusableCompilationUnit(MechaError):
    """Raised when a compilation unit lacks the necessary information to instantiate a module."""

    message: str
    compilation_unit: CompilationUnit

    def __init__(self, message: str, compilation_unit: CompilationUnit) -> None:
        super().__init__(message, compilation_unit)
        self.message = message
        self.compilation_unit = compilation_unit

    def __str__(self) -> str:
        return self.message


class Module(TextFile):
    """Class representing a bolt module."""

    scope = ("modules",)
    extension = ".bolt"


@dataclass
class CompiledModule:
    """Class holding the state of a compiled module."""

    ast: AstRoot
    code: Optional[CodeType]
    refs: List[Any]
    output: Optional[str]
    resource_location: Optional[str]
    globals: Set[str]
    namespace: JsonDict = field(default_factory=dict)
    executing: bool = False
    executed: bool = False
    execution_hooks: List[Callable[[], Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CodegenResult:
    source: Optional[str] = None
    output: Optional[str] = None
    refs: List[Any] = field(default_factory=list)


@dataclass(frozen=True)
class ModuleManager:
    """Service that manages bolt modules."""

    directory: Path = extra_field()
    database: CompilationDatabase = extra_field()
    codegen: Dispatcher[CodegenResult] = extra_field()

    registry: Dict[TextFileBase[Any], CompiledModule] = extra_field(
        default_factory=dict
    )
    stack: List[CompiledModule] = extra_field(default_factory=list)
    globals: JsonDict = extra_field(default_factory=dict)
    builtins: Set[str] = extra_field(default_factory=set)

    @property
    def path(self) -> str:
        """Return the current path."""
        if not self.stack:
            raise ValueError("No module currently executing.")
        if path := self.stack[-1].resource_location:
            return path
        raise ValueError("No resource location corresponding to the current module.")

    def get(
        self,
        target: Optional[Union[AstRoot, str]] = None,
        current: Optional[TextFileBase[Any]] = None,
    ) -> CompiledModule:
        """Retrieve an executable module."""
        if self.stack and not target and not current:
            return self.stack[-1]

        if isinstance(target, str):
            current = self.database.index[target]
        elif current is None:
            current = self.database.current

        module = self.registry.get(current)
        compilation_unit = self.database[current]
        name = compilation_unit.resource_location or "<unknown>"

        if module:
            if isinstance(target, AstRoot) and module.ast is not target:
                logger.debug("Code generation due to ast update for module %s.", name)
            else:
                return module
        else:
            if not isinstance(target, AstRoot):
                target = compilation_unit.ast
                if not target:
                    raise UnusableCompilationUnit(
                        f"No ast for module {name}.", compilation_unit
                    )
            logger.debug("Code generation for module %s.", name)

        result = self.codegen(target)

        if result.source and result.output:
            filename = (
                str(self.directory / compilation_unit.filename)
                if compilation_unit.filename
                else name
            )

            code = compile(result.source, filename, "exec")

        else:
            code = None

        module = CompiledModule(
            ast=target,
            code=code,
            refs=result.refs,
            output=result.output,
            resource_location=compilation_unit.resource_location,
            globals=set(self.globals),
        )
        self.registry[current] = module

        return module

    def eval(self, module: CompiledModule) -> AstRoot:
        """Run the module and return the output ast."""
        if not module.executed:
            module.executed = True
            for hook in module.execution_hooks:
                hook()

        if not module.code or not module.output:
            return module.ast
        if module.output in module.namespace:
            return module.namespace[module.output]

        if module.executing:
            raise ValueError("Import cycle detected.")

        logger.debug("Evaluate module %s.", module.resource_location or "<unknown>")

        module.namespace.update(self.globals)
        module.namespace["_bolt_refs"] = module.refs
        module.namespace["__name__"] = module.resource_location
        module.namespace["__file__"] = module.code.co_filename

        self.stack.append(module)
        module.executing = True

        try:
            with self.error_handler():
                exec(module.code, module.namespace)
        finally:
            module.executing = False
            self.stack.pop()

        return module.namespace[module.output]

    @contextmanager
    def error_handler(self):
        """Handle errors coming from compiled modules."""
        try:
            yield
        except Exception as exc:
            raise rewrite_traceback(exc) from None


@dataclass
class ModuleCacheBackend(AstCacheBackend):
    """Cache backend that also restores the generated modules."""

    bolt_version: str = import_from_string("bolt.__version__")

    modules: ModuleManager = required_field(repr=False, hash=False, compare=False)

    def load_data(self, f: BufferedReader) -> JsonDict:
        data = super().load_data(f)
        if data["bolt"] != self.bolt_version:
            raise ValueError("Version mismatch.")
        return data

    def dump_data(self, data: JsonDict, f: BufferedWriter):
        data["bolt"] = self.bolt_version
        super().dump_data(data, f)

    def load(self, f: BufferedReader) -> AstRoot:
        data = self.load_data(f)
        ast = data["ast"]

        if data["globals"] != set(self.modules.globals):
            raise ValueError("Globals mismatch.")

        self.modules.registry[self.modules.database.current] = CompiledModule(
            ast=ast,
            code=marshal.load(f),
            refs=data["refs"],
            output=data["output"],
            resource_location=data["resource_location"],
            globals=data["globals"],
        )

        return ast

    def dump(self, node: AstRoot, f: BufferedWriter):
        module = self.modules.get(node)

        self.dump_data(
            {
                "ast": module.ast,
                "refs": module.refs,
                "output": module.output,
                "resource_location": module.resource_location,
                "globals": module.globals,
            },
            f,
        )

        marshal.dump(module.code, f)
