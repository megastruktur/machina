"""Documentation consistency suite — release docs vs the real tree.

Card ``12-release-docs`` (P12, documentation-only): every operator-facing
reference in the RELEASE documentation must exist in the real tree. This
module is the enforcement, not prose: it fails when a documented CLI
command/flag, HTTP endpoint, Compose profile, catalogue recipe, operator
script/subcommand/flag, repo path or relative doc link points at something
the tree does not have.

Scope (deliberate):

- **Release docs** = ``README.md``, ``docs/operations/*.md``,
  ``docs/plugins/*.md`` — the surfaces this release publishes to operators.
  CLI/flag, script, Compose-profile, recipe, repo-path and doc-link checks
  run over exactly these.
- **Endpoint check** runs over ALL of ``README.md`` + ``docs/**/*.md``:
  every ``/api/v1/...`` mention anywhere must match a real FastAPI route
  (path shape, and the HTTP method when the mention carries one). Trailing-
  slash mentions (``/api/v1/auth/``) are read as router-prefix mentions and
  assert no specific route.
- The frozen P00 architecture ADRs are outside the path/CLI/script checks
  (they cite upstream material by design, e.g. SearXNG's own docs tree);
  their endpoint mentions are still validated by the global rule above.

Producers consumed (never re-implemented):

- ``mai.cli.build_parser`` / ``mai.cli.DOCUMENTED_COMMANDS`` — the one CLI
  surface (12-cli-ops);
- ``mai.api.app.app.routes`` — the real HTTP surface;
- ``compose.yaml`` profiles (parsed, not grepped);
- the three operator scripts' own ``case``-arm/usage vocabulary.

Negative controls (``-k negative``) feed synthetic fabricated references
through the same pure checkers and require a violation each — the suite's
own safety boundary: a checker that stopped failing on fabricated
references would be worthless.

No docker, no network, no filesystem mutation outside ``tmp_path``.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

RELEASE_DOC_PATHS = (
    REPO_ROOT / "README.md",
    *(sorted((REPO_ROOT / "docs" / "operations").glob("*.md"))),
    *(sorted((REPO_ROOT / "docs" / "plugins").glob("*.md"))),
)

ALL_DOC_PATHS = (
    REPO_ROOT / "README.md",
    *(sorted((REPO_ROOT / "docs").rglob("*.md"))),
)

#: Backticked tokens that start with one of these prefixes are treated as
#: repo paths and must exist. ``mai_evidence`` is campaign metadata, not the
#: product tree; dot-directories (``.venv``) are environment state.
_PATH_ROOTS = (
    "backend",
    "catalogue",
    "deploy",
    "docs",
    "experiments",
    "frontend",
    "scripts",
    "tests",
)
_ROOT_FILES = frozenset({"compose.yaml", "LICENSE", "README.md"})

#: install-host.sh / uninstall-host.sh stack-profile vocabulary: ``core`` is
#: compose's default start set (no --profile), the rest must be real profiles.
_STACK_TOKENS = frozenset({"core", "worker", "search", "memory"})


# ---------------------------------------------------------------------------
# producers: the real surfaces
# ---------------------------------------------------------------------------


def _cli_surface() -> dict[str, frozenset[str]]:
    """Subcommand (chain) -> accepted option strings, from the real parser."""
    from mai.cli import build_parser

    parser = build_parser()
    top = _subparsers_of(parser)
    assert top is not None, "mai.cli parser lost its subcommands"
    surface: dict[str, frozenset[str]] = {}
    for name, sub in top.choices.items():
        flags = _options_of(sub)
        surface[name] = flags
        nested = _subparsers_of(sub)
        if nested is not None:
            for nested_name, nested_sub in nested.choices.items():
                surface[f"{name} {nested_name}"] = flags | _options_of(nested_sub)
    return surface


def _subparsers_of(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for action in parser._actions:  # noqa: SLF001 - argparse introspection
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _options_of(parser: argparse.ArgumentParser) -> frozenset[str]:
    flags: set[str] = set()
    for action in parser._actions:  # noqa: SLF001 - argparse introspection
        flags.update(action.option_strings)
    return frozenset(flags)


def _compose_profiles() -> frozenset[str]:
    text = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    profiles: set[str] = set()
    for service in (document or {}).get("services", {}).values():
        profiles.update(service.get("profiles") or ())
    return frozenset(profiles)


def _script_flags(script: Path) -> frozenset[str]:
    """Option vocabulary from the script's own case arms + usage heredoc."""
    text = script.read_text(encoding="utf-8")
    flags = set(re.findall(r"(?m)^\s*(--[a-z0-9][a-z0-9-]*)\)", text))
    usage = re.search(r"usage\(\) \{.*?^USAGE", text, re.S | re.M)
    if usage:
        flags.update(re.findall(r"--[a-z0-9][a-z0-9-]*", usage.group(0)))
    return frozenset(flags)


def _script_subcommands(script: Path) -> frozenset[str]:
    usage = re.search(
        r"usage\(\) \{.*?^USAGE", script.read_text(encoding="utf-8"), re.S | re.M
    )
    assert usage, f"{script.name}: usage heredoc not found"
    return frozenset(re.findall(r"backup-restore\.sh\s+([a-z]+)", usage.group(0)))


def _api_routes() -> dict[str, frozenset[str]]:
    """Method -> path table from the real app, flattened the canonical way
    (same shape as backend/tests/test_health.py route enumeration)."""
    from mai.api.app import create_app

    app = create_app(env={})
    routes: dict[str, frozenset[str]] = {}
    for included in app.routes:
        nested = getattr(included, "original_router", None)
        for route in nested.routes if nested is not None else [included]:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None)
            if isinstance(path, str) and path.startswith("/api/v1") and methods:
                routes[path] = routes.get(path, frozenset()) | frozenset(methods)
    return routes


# ---------------------------------------------------------------------------
# doc readers: fenced blocks joined across ``\`` continuations + inline spans
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.S | re.M)
_SPAN_RE = re.compile(r"`([^`\n]+)`")


def _doc_segments(text: str) -> Iterator[tuple[str, bool]]:
    """Yield (segment, is_fenced) — fenced blocks first, then inline spans
    found OUTSIDE fences (so fence content is not double-reported)."""
    consumed: list[tuple[int, int]] = []
    for match in _FENCE_RE.finditer(text):
        consumed.append(match.span())
        for line in _logical_lines(match.group(1)):
            yield line, True
    position = 0
    pieces: list[str] = []
    for start, end in consumed:
        pieces.append(text[position:start])
        position = end
    pieces.append(text[position:])
    outside = "".join(pieces)
    for match in _SPAN_RE.finditer(outside):
        yield match.group(1), False


def _logical_lines(block: str) -> Iterator[str]:
    pending = ""
    for line in block.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        yield pending + line
        pending = ""
    if pending:
        yield pending


# ---------------------------------------------------------------------------
# reference extractors (pure: text -> findings)
# ---------------------------------------------------------------------------

_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*)\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9-]*")
_SCRIPT_RE = re.compile(
    r"(?:sudo\s+)?(?:bash\s+)?scripts/"
    r"(install-host|uninstall-host|backup-restore)\.sh"
)
_COMPOSE_RE = re.compile(r"(?<![\w.-])docker\s+compose\b")
_MAI_CONSOLE_RE = re.compile(r"(?<![\w./-])mai\s+(?=[a-z])")
_MAI_MODULE_RE = re.compile(r"(?:python\s+-m\s+)?mai\.cli\b")
_METHOD_PATH_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/api/v1/[A-Za-z0-9_/.\-{}<>]+)"
)
_BARE_PATH_RE = re.compile(r"(?<![\w\-])(/api/v1/[A-Za-z0-9_/.\-{}<>]+)")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def _check_cli(text: str, surface: dict[str, frozenset[str]]) -> list[str]:
    problems: list[str] = []
    for segment, fenced in _doc_segments(text):
        where = "fence" if fenced else "span"
        for module in _MAI_MODULE_RE.finditer(segment):
            rest = segment[module.end() :]
            problems += _check_cli_tokens(rest, surface, where, segment)
        for console in _MAI_CONSOLE_RE.finditer(segment):
            rest = segment[console.end() :]
            problems += _check_cli_tokens(rest, surface, where, segment)
    return problems


def _check_cli_tokens(
    rest: str, surface: dict[str, frozenset[str]], where: str, segment: str
) -> list[str]:
    tokens = rest.split()
    if not tokens or not (match := _WORD_RE.match(tokens[0])):
        return []  # bare ``python -m mai.cli`` mention: no command asserted
    chain = [match.group(0)]
    index = 1
    while index < len(tokens) and " ".join(chain + [tokens[index]]) in surface:
        chain.append(tokens[index])
        index += 1
    key = " ".join(chain)
    if key not in surface:
        registered = ", ".join(sorted(name for name in surface if " " not in name))
        return [
            f"CLI command '{key}' ({where}) is not registered; documented set: {registered}"
        ]
    problems = []
    for flag in _FLAG_RE.findall(" ".join(tokens[index:])):
        if flag not in surface[key]:
            return [f"CLI flag '{flag}' is not accepted by '{key}' ({where})"]
    return problems


def _check_scripts(
    text: str,
    flags_by_script: dict[str, frozenset[str]],
    subs_by_script: dict[str, frozenset[str]],
    profiles: frozenset[str],
) -> list[str]:
    problems: list[str] = []
    for segment, fenced in _doc_segments(text):
        where = "fence" if fenced else "span"
        for match in _SCRIPT_RE.finditer(segment):
            name = match.group(1)
            rest = segment[match.end() :].split()
            index = 0
            if name == "backup-restore":
                if not rest or not (word := _WORD_RE.match(rest[0])):
                    continue
                sub = word.group(0)
                if sub not in subs_by_script[name]:
                    problems.append(
                        f"script subcommand 'backup-restore.sh {sub}' ({where}) "
                        f"does not exist; real: {sorted(subs_by_script[name])}"
                    )
                    continue
                index = 1
            tokens = rest[index:]
            for position, raw in enumerate(tokens):
                flag = raw.strip("[]'\"")
                if not flag.startswith("--"):
                    continue
                if flag not in flags_by_script[name]:
                    problems.append(
                        f"script flag '{flag}' is not accepted by {name}.sh ({where})"
                    )
                if flag == "--stack-profiles" and position + 1 < len(tokens):
                    problems += _check_stack_tokens(
                        tokens[position + 1], profiles, where
                    )
    return problems


def _check_stack_tokens(value: str, profiles: frozenset[str], where: str) -> list[str]:
    problems = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if token not in _STACK_TOKENS:
            problems.append(
                f"stack profile token '{token}' ({where}) is not one of "
                f"{sorted(_STACK_TOKENS)}"
            )
        elif token != "core" and token not in profiles:
            problems.append(
                f"stack profile token '{token}' ({where}) is not a compose.yaml profile"
            )
    return problems


def _check_compose_profiles(text: str, profiles: frozenset[str]) -> list[str]:
    problems: list[str] = []
    for segment, fenced in _doc_segments(text):
        where = "fence" if fenced else "span"
        if not _COMPOSE_RE.search(segment):
            continue
        for match in re.finditer(r"--profile\s+(\S+)", segment):
            value = match.group(1)
            if value not in profiles:
                problems.append(
                    f"compose profile '{value}' ({where}) does not exist in "
                    f"compose.yaml; real profiles: {sorted(profiles)}"
                )
    return problems


def _route_matches(doc_path: str, route_path: str) -> bool:
    doc_parts = _wildcard_segments(doc_path)
    route_parts = _wildcard_segments(route_path)
    return len(doc_parts) == len(route_parts) and all(
        doc == real or doc == "*" or real == "*"
        for doc, real in zip(doc_parts, route_parts)
    )


def _wildcard_segments(path: str) -> list[str]:
    parts = [p for p in path.split("/") if p != ""]
    return [
        "*"
        if (p.startswith("{") and p.endswith("}"))
        or (p.startswith("<") and p.endswith(">"))
        else p
        for p in parts
    ]


def _check_endpoints(text: str, routes: dict[str, frozenset[str]]) -> list[str]:
    problems: list[str] = []

    def look(mention: str, method: str | None, where: str) -> None:
        cleaned = mention.rstrip(".,;:")
        if cleaned.endswith("/") or len(_wildcard_segments(cleaned)) <= 2:
            return  # router-prefix mention, asserts no specific route
        matching = [path for path in routes if _route_matches(cleaned, path)]
        if not matching:
            problems.append(f"endpoint '{cleaned}' ({where}) matches no /api/v1 route")
            return
        if method is not None and not any(method in routes[path] for path in matching):
            allowed = sorted(set().union(*(routes[path] for path in matching)))
            problems.append(
                f"endpoint '{cleaned}' ({where}) does not accept {method}; "
                f"real methods: {allowed}"
            )

    for match in _METHOD_PATH_RE.finditer(text):
        look(match.group(2), match.group(1), "method mention")
    for match in _BARE_PATH_RE.finditer(text):
        look(match.group(1), None, "path mention")
    return problems


def _outside_fence_text(text: str) -> str:
    consumed: list[tuple[int, int]] = []
    for match in _FENCE_RE.finditer(text):
        consumed.append(match.span())
    position = 0
    pieces: list[str] = []
    for start, end in consumed:
        pieces.append(text[position:start])
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _check_repo_paths(text: str) -> list[str]:
    outside = _outside_fence_text(text)
    problems: list[str] = []
    for token in _SPAN_RE.findall(outside):
        problems += _check_one_path(token)
    for line in outside.splitlines():
        problems += _check_recipes(line)
    return problems


def _check_one_path(token: str) -> list[str]:
    if any(ch.isspace() for ch in token):
        return []  # command line, not a path
    if token in _ROOT_FILES:
        exists = (REPO_ROOT / token).exists()
    elif not token.startswith(_PATH_ROOTS) or "/" not in token:
        return []
    else:
        cleaned = token.rstrip("/")
        if any(part.startswith(".") for part in cleaned.split("/")):
            return []
        exists = (REPO_ROOT / cleaned).exists()
    return [] if exists else [f"referenced path '{token}' does not exist in the tree"]


def _check_recipes(segment: str) -> list[str]:
    if "catalogue" not in segment:
        return []
    problems: list[str] = []
    for token in _SPAN_RE.findall(segment):
        if token.endswith(".yaml") and "/" not in token:
            if not (REPO_ROOT / "catalogue" / token).exists():
                problems.append(
                    f"referenced recipe '{token}' does not exist in catalogue/"
                )
    return problems


def _check_doc_links(doc: Path, text: str) -> list[str]:
    problems: list[str] = []
    for target in _MD_LINK_RE.findall(text):
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        resolved = (doc.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            problems.append(f"broken relative link '{target}'")
    return problems


# ---------------------------------------------------------------------------
# the real-tree checks (02)
# ---------------------------------------------------------------------------


def test_release_docs_exist_and_are_the_published_set() -> None:
    names = sorted(str(path.relative_to(REPO_ROOT)) for path in RELEASE_DOC_PATHS)
    assert names == [
        "README.md",
        "docs/operations/acceptance.md",
        "docs/operations/quickstart.md",
        "docs/operations/recovery.md",
        "docs/operations/uninstall.md",
        "docs/plugins/quickstart.md",
    ]


def test_documented_cli_commands_and_flags_exist() -> None:
    from mai.cli import DOCUMENTED_COMMANDS

    surface = _cli_surface()
    assert set(surface) == set(DOCUMENTED_COMMANDS) | {"admin write", "admin delete"}
    for doc in RELEASE_DOC_PATHS:
        problems = _check_cli(doc.read_text(encoding="utf-8"), surface)
        assert not problems, f"{doc.name}: {problems}"


def test_documented_script_subcommands_flags_and_stack_profiles_exist() -> None:
    flags_by_script = {
        name: _script_flags(REPO_ROOT / "scripts" / f"{name}.sh")
        for name in ("install-host", "uninstall-host", "backup-restore")
    }
    subs_by_script = {
        "backup-restore": _script_subcommands(
            REPO_ROOT / "scripts" / "backup-restore.sh"
        )
    }
    assert subs_by_script["backup-restore"] == {"backup", "verify", "restore"}
    profiles = _compose_profiles()
    assert profiles == {"worker", "search", "memory"}
    for doc in RELEASE_DOC_PATHS:
        text = doc.read_text(encoding="utf-8")
        problems = _check_scripts(
            text, flags_by_script, subs_by_script, profiles
        ) + _check_compose_profiles(text, profiles)
        assert not problems, f"{doc.name}: {problems}"


def test_documented_endpoints_exist_with_documented_methods() -> None:
    routes = _api_routes()
    assert routes, "the API surface vanished"
    for doc in ALL_DOC_PATHS:
        problems = _check_endpoints(doc.read_text(encoding="utf-8"), routes)
        assert not problems, f"{doc.relative_to(REPO_ROOT)}: {problems}"


def test_documented_paths_recipes_and_links_exist() -> None:
    for doc in RELEASE_DOC_PATHS:
        text = doc.read_text(encoding="utf-8")
        problems = _check_repo_paths(text) + _check_doc_links(doc, text)
        assert not problems, f"{doc.name}: {problems}"


# ---------------------------------------------------------------------------
# negative controls (03): fabricated references MUST be refused
# ---------------------------------------------------------------------------


def _fabricated(text: str, tmp_path: Path, name: str = "fabricated.md") -> str:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return text


def test_negative_unknown_cli_command_is_refused(tmp_path: Path) -> None:
    surface = _cli_surface()
    problems = _check_cli(
        _fabricated("`python -m mai.cli teleport --data-root /tmp`", tmp_path),
        surface,
    )
    assert problems and "not registered" in problems[0]


def test_negative_unknown_cli_flag_is_refused(tmp_path: Path) -> None:
    surface = _cli_surface()
    problems = _check_cli(
        _fabricated("`mai.cli status --frobnicate --data-root /tmp`", tmp_path),
        surface,
    )
    assert problems and "--frobnicate" in problems[0]


def test_negative_unknown_endpoint_is_refused(tmp_path: Path) -> None:
    routes = _api_routes()
    problems = _check_endpoints(
        _fabricated("`GET /api/v1/nosuchroute`", tmp_path), routes
    )
    assert problems and "matches no" in problems[0]


def test_negative_wrong_endpoint_method_is_refused(tmp_path: Path) -> None:
    routes = _api_routes()
    problems = _check_endpoints(
        _fabricated("`DELETE /api/v1/health`", tmp_path), routes
    )
    assert problems and "does not accept DELETE" in problems[0]


def test_negative_unknown_compose_profile_is_refused(tmp_path: Path) -> None:
    profiles = _compose_profiles()
    problems = _check_compose_profiles(
        _fabricated("`docker compose --profile nosuch up -d`", tmp_path), profiles
    )
    assert problems and "nosuch" in problems[0]


def test_negative_unknown_script_flag_is_refused(tmp_path: Path) -> None:
    flags = {"install-host": _script_flags(REPO_ROOT / "scripts" / "install-host.sh")}
    problems = _check_scripts(
        _fabricated("`sudo bash scripts/install-host.sh --frobnicate`", tmp_path),
        flags,
        {},
        _compose_profiles(),
    )
    assert problems and "--frobnicate" in problems[0]


def test_negative_unknown_script_subcommand_is_refused(tmp_path: Path) -> None:
    subs = {
        "backup-restore": _script_subcommands(
            REPO_ROOT / "scripts" / "backup-restore.sh"
        )
    }
    problems = _check_scripts(
        _fabricated("`scripts/backup-restore.sh teleport --data-root /tmp`", tmp_path),
        {"backup-restore": _script_flags(REPO_ROOT / "scripts" / "backup-restore.sh")},
        subs,
        _compose_profiles(),
    )
    assert problems and "teleport" in problems[0]


def test_negative_dangling_repo_path_is_refused(tmp_path: Path) -> None:
    problems = _check_repo_paths(
        _fabricated("see `scripts/no-such-operator-script.sh` for details", tmp_path)
    )
    assert problems and "no-such-operator-script.sh" in problems[0]


def test_negative_dangling_recipe_is_refused(tmp_path: Path) -> None:
    problems = _check_repo_paths(
        _fabricated("catalogue recipes: `nosuch-recipe.yaml`", tmp_path)
    )
    assert problems and "nosuch-recipe.yaml" in problems[0]


def test_negative_dangling_doc_link_is_refused(tmp_path: Path) -> None:
    problems = _check_doc_links(
        tmp_path / "fabricated.md",
        _fabricated("see [the missing doc](no-such-doc.md)", tmp_path),
    )
    assert problems and "no-such-doc.md" in problems[0]
