# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2024-present repod contributors
# See LICENSE for terms. Commercial use: LICENSE-COMMERCIAL.md
"""
Module : test_no_import_side_effects.py
Rôle   : garde-fou contre la régression qui a cassé la CI (7 erreurs de
         collecte pytest). services/component_sbom.py créait SBOM_DIR
         (/repos/sboms par défaut) au niveau module ; routers/upload.py
         l'importe et routers/__init__.py importe upload, donc le moindre
         `import routers` tentait un mkdir sur /repos et levait
         PermissionError sur un runner GitHub. Toute la collecte échouait
         avant qu'un seul test ne tourne.

         Le correctif est une convention : importer un module ne doit jamais
         toucher au filesystem. Les répertoires sont créés à la première
         écriture. Ce test verrouille la convention pour les prochains
         X_DIR ajoutés, sinon la panne réapparaît telle quelle.

         Analyse purement statique (ast) : ce module n'importe aucun des
         modules inspectés, ce qui serait précisément l'effet de bord testé.

Dépend : ast, pathlib (aucune dépendance applicative)
"""
import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_PACKAGES = ("services", "routers", "auth", "db")

# Appels considérés comme touchant le filesystem au niveau module.
_FS_WRITERS = frozenset({"mkdir", "makedirs", "touch", "write_text", "write_bytes"})


def _module_level_fs_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """Retourne les (nom_appel, ligne) exécutés à l'import du module.

    On descend dans les `for`/`if`/`try`/`with` de premier niveau (le bug
    d'origine était un `for _d in [...]: _d.mkdir(...)`) mais jamais dans un
    corps de fonction ou de classe : là, le code ne s'exécute pas à l'import.
    """
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr in _FS_WRITERS:
                    found.append((func.attr, child.lineno))
            visit(child)

    visit(tree)
    return found


def _python_sources() -> list[Path]:
    return sorted(
        path
        for package in _PACKAGES
        for path in (_BACKEND / package).rglob("*.py")
    )


@pytest.mark.parametrize(
    "source", _python_sources(), ids=lambda p: str(p.relative_to(_BACKEND))
)
def test_module_import_does_not_touch_filesystem(source: Path):
    """Aucune écriture disque au niveau module dans services/ routers/ auth/ db/."""
    calls = _module_level_fs_calls(ast.parse(source.read_text(encoding="utf-8")))
    assert not calls, (
        f"{source.relative_to(_BACKEND)} écrit sur le disque à l'import : "
        + ", ".join(f"{name}() ligne {line}" for name, line in calls)
        + ". Déplacer l'appel dans la fonction qui écrit (voir "
        "services/component_sbom.py). Un mkdir au niveau module rend "
        "`import routers` impossible là où /repos n'est pas inscriptible et "
        "casse toute la collecte pytest."
    )


def test_guard_detects_a_module_level_mkdir():
    """Le détecteur voit bien le motif exact qui avait cassé la CI."""
    source = (
        "from pathlib import Path\n"
        "SBOM_DIR = Path('/repos/sboms')\n"
        "SBOM_DIR.mkdir(parents=True, exist_ok=True)\n"
    )
    assert _module_level_fs_calls(ast.parse(source)) == [("mkdir", 3)]


def test_guard_detects_a_mkdir_inside_a_module_level_loop():
    """Variante routers/upload.py : mkdir dans une boucle de premier niveau."""
    source = (
        "from pathlib import Path\n"
        "DIRS = [Path('/repos/pool')]\n"
        "for _d in DIRS:\n"
        "    _d.mkdir(parents=True, exist_ok=True)\n"
    )
    assert _module_level_fs_calls(ast.parse(source)) == [("mkdir", 4)]


def test_guard_allows_a_mkdir_inside_a_function():
    """Un mkdir différé dans une fonction est la forme attendue."""
    source = (
        "from pathlib import Path\n"
        "POOL = Path('/repos/pool')\n"
        "def save(name):\n"
        "    POOL.mkdir(parents=True, exist_ok=True)\n"
        "    (POOL / name).write_text('x')\n"
    )
    assert _module_level_fs_calls(ast.parse(source)) == []
