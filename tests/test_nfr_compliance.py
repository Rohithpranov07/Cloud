"""Automated enforcement of the TRD §5 non-functional requirements.

T8.1 audits these by hand. Encoding them as tests means they stay true as the repo
changes, rather than being true only on the day someone ran grep.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.config import ARCHETYPES, PRIMITIVE_MAP, PRIMITIVE_UNIT_CAPACITY, UNIT_COST_PER_MIN

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# CLAUDE.md import-direction boundary: a pipeline package may import only from packages
# EARLIER in this list, never sideways and never backward from evaluation.
PIPELINE_ORDER = [
    "trace_gen",
    "classifier",
    "forecaster",
    "controller",
    "cost_governance",
    "feedback",
    "evaluation",
]

SOURCE_FILES = sorted(SRC.rglob("*.py"))


def _module_package(path: Path) -> str | None:
    relative = path.relative_to(SRC)
    return relative.parts[0] if len(relative.parts) > 1 else None


# --------------------------------------------------------------------------------
# NFR-3 — no silent failures
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_nfr3_no_bare_except(path: Path) -> None:
    """No bare `except:` and no blanket `except Exception:` anywhere in src/."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert node.type is not None, f"{path}:{node.lineno} bare except"
            assert not (
                isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
            ), f"{path}:{node.lineno} blanket except swallows errors"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_nfr3_no_silent_pass_in_error_paths(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert not all(isinstance(stmt, ast.Pass) for stmt in node.body), (
                f"{path}:{node.lineno} exception handler silently passes"
            )


# --------------------------------------------------------------------------------
# NFR-4 — full type hints
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_nfr4_every_function_is_fully_annotated(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assert node.returns is not None, f"{path}:{node.lineno} {node.name} has no return type"
        for argument in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            if argument.arg == "self":
                continue
            assert argument.annotation is not None, (
                f"{path}:{node.lineno} {node.name}({argument.arg}) has no type annotation"
            )


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_type_ignore_comments(path: Path) -> None:
    """mypy exceptions live in mypy.ini with a written justification, not scattered inline."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        assert "type: ignore" not in line, f"{path}:{number} inline type: ignore"


# --------------------------------------------------------------------------------
# CLAUDE.md module boundaries
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_import_direction_is_never_sideways_or_backward(path: Path) -> None:
    """A pipeline package may import only from packages earlier in PIPELINE_ORDER."""
    package = _module_package(path)
    if package is None or package not in PIPELINE_ORDER:
        return
    own_rank = PIPELINE_ORDER.index(package)

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        module_name: str | None = None
        if isinstance(node, ast.ImportFrom):
            module_name = node.module
            line_number = node.lineno
        elif isinstance(node, ast.Import):
            module_name = node.names[0].name
            line_number = node.lineno
        else:
            continue
        if not module_name or not module_name.startswith("src."):
            continue

        imported = module_name.split(".")[1]
        if imported not in PIPELINE_ORDER:  # src.config is a shared root helper
            continue
        imported_rank = PIPELINE_ORDER.index(imported)
        assert imported_rank < own_rank, (
            f"{path}:{line_number} '{package}' imports from '{imported}', which is not earlier "
            "in the pipeline (CLAUDE.md module boundaries)"
        )


# --------------------------------------------------------------------------------
# Anti-Hallucination rule 3 — one definition per concept
# --------------------------------------------------------------------------------

def test_constants_are_defined_exactly_once() -> None:
    """The TRD §1.1/§1.6 constants must exist only in src/config.py."""
    for constant in ("ARCHETYPES", "PRIMITIVE_MAP", "PRIMITIVE_UNIT_CAPACITY", "UNIT_COST_PER_MIN"):
        definitions = [
            path
            for path in SOURCE_FILES
            if re.search(rf"^{constant}\s*[:=]", path.read_text(encoding="utf-8"), re.MULTILINE)
        ]
        assert definitions == [SRC / "config.py"], (
            f"{constant} is defined in {definitions}; it must live only in src/config.py"
        )


def test_trd_fixed_values_are_intact() -> None:
    """Anti-Drift rule 8: the TRD §1.1 and §1.6 tables must not have drifted."""
    assert ARCHETYPES == [
        "short_conversational",
        "long_context_rag",
        "agentic_tool_using",
        "batch_offline",
    ]
    assert PRIMITIVE_MAP == {
        "short_conversational": "bedrock_on_demand",
        "long_context_rag": "sagemaker_endpoint",
        "agentic_tool_using": "eks_gpu_reserved",
        "batch_offline": "eks_gpu_spot",
    }
    assert PRIMITIVE_UNIT_CAPACITY == {
        "bedrock_on_demand": 30,
        "sagemaker_endpoint": 15,
        "eks_gpu_reserved": 10,
        "eks_gpu_spot": 12,
    }
    assert UNIT_COST_PER_MIN == {
        "bedrock_on_demand": 0.02,
        "sagemaker_endpoint": 0.05,
        "eks_gpu_reserved": 0.09,
        "eks_gpu_spot": 0.04,
    }


# --------------------------------------------------------------------------------
# NFR-2 — every stochastic function is seeded and has a determinism test
# --------------------------------------------------------------------------------

STOCHASTIC_FUNCTIONS = [
    ("src/trace_gen/generator.py", "generate_trace"),
    ("src/classifier/archetype_classifier.py", "add_proxy_features"),
    ("src/classifier/archetype_classifier.py", "train_classifier"),
    ("src/feedback/recalibration.py", "recalibrate"),
]


@pytest.mark.parametrize(("relative_path", "function_name"), STOCHASTIC_FUNCTIONS)
def test_nfr2_stochastic_functions_accept_a_seed(relative_path: str, function_name: str) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            names = [a.arg for a in node.args.args + node.args.kwonlyargs]
            assert "seed" in names or "cfg" in names, (
                f"{relative_path}:{function_name} does not accept a seed (NFR-2)"
            )
            return
    pytest.fail(f"{function_name} not found in {relative_path}")


def test_nfr2_global_numpy_random_state_is_never_used() -> None:
    """Seeding must be via np.random.default_rng, never the legacy global state."""
    forbidden = re.compile(r"np\.random\.(seed|rand|randn|randint|choice|normal|poisson)\(")
    for path in SOURCE_FILES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            assert not forbidden.search(line), (
                f"{path}:{number} uses numpy's global random state; use np.random.default_rng"
            )


# --------------------------------------------------------------------------------
# NFR-1 — reproducibility
# --------------------------------------------------------------------------------

def test_nfr1_all_third_party_imports_are_pinned_in_requirements() -> None:
    """Nothing may be imported that a fresh `pip install -r requirements.txt` would miss."""
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pinned = {
        line.split("==")[0].strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    distribution_names = {"yaml": "pyyaml", "sklearn": "scikit-learn"}
    standard_library = {"__future__", "ast", "dataclasses", "pathlib", "re", "typing", "warnings"}

    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
            elif isinstance(node, ast.Import):
                top = node.names[0].name.split(".")[0]
            else:
                continue
            if top in standard_library or top == "src":
                continue
            assert distribution_names.get(top, top).lower() in pinned, (
                f"{path} imports '{top}', which is not pinned in requirements.txt"
            )


def test_nfr1_requirements_are_all_exact_pins() -> None:
    """TRD §4: pin with ==, never >=."""
    for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "==" in stripped, f"requirements.txt line is not an exact pin: {stripped!r}"
        assert ">=" not in stripped and "~=" not in stripped
