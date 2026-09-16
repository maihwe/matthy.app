"""Static ratchets — S5 and S6 of 10 §11.

Two rules that a reviewer would catch by reading and CI cannot catch by
running:

* **S5** — no serializer uses ``fields = '__all__'``. A serializer with all
  fields exposed publishes every column the model grows later, including ones
  added by a migration nobody connected to the API. It is the mechanism by
  which ``password_hash`` or an internal ``notes`` column reaches a client.
* **S6** — every DRF view declares its own ``permission_classes``. Inheriting
  the global default is not a decision, and 05 §8 requires a decision per
  endpoint.

These are **source ratchets, not behaviour tests**: they must fail on code that
has not been written yet, which is the entire point. That creates a failure
mode worth guarding against explicitly — a ratchet whose detector is broken
reports "0 violations" and looks identical to a clean codebase. So each
detector is self-tested against planted violations in
``test_the_ratchets_can_actually_fail``. A ratchet that cannot fail is worth
less than no ratchet, because it is believed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]

# Not sources of the project's own behaviour: vendored code, generated
# migrations, and the caches a previous run left behind.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "migrations",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "htmlcov",
    }
)

# DRF's own view classes. Matched on the *base name* rather than the import
# path, so `from rest_framework.views import APIView` and
# `from rest_framework import generics` + `generics.ListCreateAPIView` are
# both recognised without tracking imports.
DRF_BASE_NAMES = frozenset(
    {
        "APIView",
        "GenericAPIView",
        "GenericViewSet",
        "ViewSet",
        "ModelViewSet",
        "ReadOnlyModelViewSet",
        "ListAPIView",
        "CreateAPIView",
        "RetrieveAPIView",
        "UpdateAPIView",
        "DestroyAPIView",
        "ListCreateAPIView",
        "RetrieveUpdateAPIView",
        "RetrieveDestroyAPIView",
        "RetrieveUpdateDestroyAPIView",
    }
)

# Project base classes that declare `permission_classes` once, on purpose.
# A view inheriting one of these has made a decision; a view inheriting
# `APIView` directly has not. Empty until S4 introduces the first one.
PERMISSION_DECIDING_BASES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        try:
            shown = self.path.relative_to(REPO_ROOT)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}: {self.message}"


def _python_sources() -> list[Path]:
    """Every project Python file, excluding vendored and generated code."""
    sources: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        sources.append(path)
    return sources


def _base_names(node: ast.ClassDef) -> set[str]:
    """Base class names, with any module prefix stripped.

    ``rest_framework.views.APIView`` and ``APIView`` both reduce to ``APIView``.
    A subscripted base (``GenericAPIView[SomeModel]``) is unwrapped to its
    value, since that form is legal in the codebase's style.
    """
    names: set[str] = set()
    for base in node.bases:
        target = base.value if isinstance(base, ast.Subscript) else base
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _assigned_names(node: ast.ClassDef) -> set[str]:
    """Names assigned at the class's own top level.

    Only the class body: an assignment inside ``get_queryset`` is a local, not
    a declaration, and treating it as one would let a view appear compliant.
    """
    names: set[str] = set()
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names.add(statement.target.id)
    return names


def _method_names(node: ast.ClassDef) -> set[str]:
    return {s.name for s in node.body if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)}


def find_all_fields_serializers(tree: ast.AST, path: Path) -> list[Violation]:
    """S5 — serializers declaring ``fields = '__all__'``."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in ast.walk(node):
            # The literal `'__all__'` is unambiguous: nothing else in a Django
            # codebase assigns that string to a name.
            if not isinstance(statement, ast.Assign):
                continue
            if not isinstance(statement.value, ast.Constant):
                continue
            if statement.value.value != "__all__":
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == "fields":
                    violations.append(
                        Violation(
                            path,
                            statement.lineno,
                            f"{node.name} declares fields = '__all__'; list the fields "
                            "explicitly so a new column is not published by default",
                        )
                    )
    return violations


def find_undeclared_permission_views(tree: ast.AST, path: Path) -> list[Violation]:
    """S6 — DRF views that do not declare their own permissions."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        bases = _base_names(node)
        is_drf_view = bool(bases & DRF_BASE_NAMES) or any(
            base.endswith("ViewSet") for base in bases
        )
        if not is_drf_view:
            continue

        if bases & PERMISSION_DECIDING_BASES:
            continue

        assigned = _assigned_names(node)
        if "permission_classes" in assigned:
            continue

        # An override of `get_permissions()` is also a decision — it is the
        # form used when permission depends on the object being acted on.
        if "get_permissions" in _method_names(node):
            continue

        violations.append(
            Violation(
                path,
                node.lineno,
                f"{node.name} declares no permission_classes and no get_permissions(); "
                "05 §8 requires each endpoint to state its own requirement",
            )
        )
    return violations


def _scan(checker, sources: list[Path]) -> list[Violation]:
    """Run a checker over real files, skipping anything that will not parse.

    A syntax error is ruff's and mypy's job to report (gates 1-3), not this
    test's; failing here would report it twice under a misleading name.
    """
    violations: list[Violation] = []
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        violations.extend(checker(tree, path))
    return violations


class TestSourceCoverage:
    """The ratchets are only meaningful over non-empty input.

    If the walker silently returned nothing — a wrong root, an over-broad
    SKIP_DIRS — both ratchets below would pass while inspecting no code at all.
    """

    def test_sources_are_found(self) -> None:
        sources = _python_sources()
        assert sources, f"no Python sources found under {REPO_ROOT}"
        assert any("settings" in p.parts for p in sources), (
            "config/settings was not walked; the root or SKIP_DIRS is wrong"
        )

    def test_generated_and_vendored_code_is_excluded(self) -> None:
        for path in _python_sources():
            parts = set(path.relative_to(REPO_ROOT).parts)
            assert not (parts & SKIP_DIRS), f"{path} should have been excluded"


class TestS5NoWildcardSerializerFields:
    """10 §11 S5 — a new column must not be published by default."""

    def test_no_serializer_uses_fields_all(self) -> None:
        violations = _scan(find_all_fields_serializers, _python_sources())
        assert not violations, "fields = '__all__' found:\n" + "\n".join(
            str(v) for v in violations
        )


class TestS6EveryViewDeclaresPermissions:
    """10 §11 S6, 05 §8 — an endpoint's permission requirement is stated."""

    def test_every_view_declares_permission_classes(self) -> None:
        violations = _scan(find_undeclared_permission_views, _python_sources())
        assert not violations, "views without a permission decision:\n" + "\n".join(
            str(v) for v in violations
        )


class TestTheRatchetsCanActuallyFail:
    """Prove each detector fires on a planted violation.

    Without this, a detector with a bug — a wrong node type, a name that never
    matches — reports zero violations and is indistinguishable from a clean
    codebase. The planted source below is the exact shape the real check must
    catch, so these tests fail if the check is defanged by a later refactor.
    """

    def test_s5_detector_catches_fields_all(self) -> None:
        source = "from rest_framework import serializers\n\n\nclass UserSerializer(serializers.ModelSerializer):\n    class Meta:\n        model = User\n        fields = '__all__'\n"
        found = find_all_fields_serializers(ast.parse(source), Path("planted.py"))
        assert len(found) == 1
        assert "UserSerializer" in found[0].message
        assert found[0].line == 7

    def test_s5_detector_allows_an_explicit_field_list(self) -> None:
        source = "class UserSerializer(serializers.ModelSerializer):\n    class Meta:\n        fields = ['id', 'email']\n"
        assert find_all_fields_serializers(ast.parse(source), Path("planted.py")) == []

    def test_s6_detector_catches_a_bare_api_view(self) -> None:
        source = "class GroupsView(APIView):\n    def get(self, request):\n        return Response({})\n"
        found = find_undeclared_permission_views(ast.parse(source), Path("planted.py"))
        assert len(found) == 1
        assert "GroupsView" in found[0].message

    def test_s6_detector_allows_a_declared_view(self) -> None:
        source = "class GroupsView(APIView):\n    permission_classes = [IsAuthenticated]\n"
        assert find_undeclared_permission_views(ast.parse(source), Path("planted.py")) == []

    def test_s6_detector_allows_a_get_permissions_override(self) -> None:
        """Object-dependent permission cannot be a class attribute."""
        source = "class GroupDetailView(APIView):\n    def get_permissions(self):\n        return [IsGroupMember()]\n"
        assert find_undeclared_permission_views(ast.parse(source), Path("planted.py")) == []

    def test_s6_detector_ignores_non_views(self) -> None:
        """A plain class named like a view is not a view.

        Matching on the name alone would flag `class GroupViewSetSerializer`,
        and a ratchet with false positives gets an exemption added to it,
        which is how a ratchet dies.
        """
        source = "class GroupViewSetSerializer(Serializer):\n    pass\n"
        assert find_undeclared_permission_views(ast.parse(source), Path("planted.py")) == []

    def test_s6_detector_catches_an_undeclared_viewset(self) -> None:
        source = "class GroupViewSet(ModelViewSet):\n    queryset = Group.objects.all()\n"
        found = find_undeclared_permission_views(ast.parse(source), Path("planted.py"))
        assert len(found) == 1

    def test_s6_detector_sees_through_a_subscripted_base(self) -> None:
        """``GenericAPIView[Group]`` is the same class as ``GenericAPIView``."""
        source = "class GroupView(GenericAPIView[Group]):\n    pass\n"
        found = find_undeclared_permission_views(ast.parse(source), Path("planted.py"))
        assert len(found) == 1

    def test_s6_detector_does_not_accept_a_local_assignment(self) -> None:
        """A ``permission_classes`` assigned inside a method is not a declaration."""
        source = "class GroupView(APIView):\n    def get(self, request):\n        permission_classes = []\n        return Response({})\n"
        found = find_undeclared_permission_views(ast.parse(source), Path("planted.py"))
        assert len(found) == 1
