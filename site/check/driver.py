"""The repo checker's Python side: it runs in Pyodide, in a Web Worker (worker.mjs), on files the worker fetched.

The worker does every network request (GitHub and PyPI, in parallel, from the browser); this module only reads the
files it was given, with wheelgap's own functions, so the page says what `uvx wheelgap` would say on the same lock:

- plan(paths): from the repo's file list, which files to fetch.
- detect(root): the targets and Pythons the repo's workflows and Dockerfiles name (wheelgap --suggest-targets).
- check(root, lockfile, ...): the lockfile's gaps, offline, from the files the lock lists.
- gap_wheels(list): which of the last check's gaps have a verified wheel (wheelgap --fix).
- fixes(releases, wanted): for each gap, the nearest newer release with a wheel (wheelgap --suggest).

Every function takes and returns JSON text, so nothing depends on how Pyodide converts objects.
tests/test_check_driver.py runs it under CPython with the wheelgap version worker.mjs pins.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import wheelgap
from packaging.utils import canonicalize_name
from wheelgap import api, offline, origin, pypi, suggest_targets
from wheelgap import suggest as release_suggest
from wheelgap.check import Row, Status
from wheelgap.sources import LOCKFILES, Source, SourceError, load_file, pythons_outside_lock
from wheelgap.targets import DEFAULT_PLATFORMS, NEWEST_PYTHON, Target, TargetError

# The lockfiles that list each package's files, so they can be checked without asking an index. The other
# checkable files (Pipfile.lock, requirements files, pixi.lock, pyproject.toml) don't, so the page checks them
# by looking each package up on PyPI from the browser, the way the CLI does: needs() lists the lookups and the
# worker fetches them, then check() fills the packages from the answers. A pyproject.toml lists direct
# requirements only, as `uvx wheelgap requirements.txt` checks them. And even a listed lockfile can leave a
# package's files out; check() catches that below.
OFFLINE = ("uv.lock", "pylock.toml", "poetry.lock", "pdm.lock")
LOOKUPS = tuple(name for name in LOCKFILES if name not in OFFLINE) + ("pyproject.toml",)
GAPS = (Status.SOURCE, Status.MISSING)  # no wheel; UNKNOWN (couldn't be checked) is reported apart
MAX_LOCKFILES = 30
MAX_LOOKUPS = 300
MAX_DOCKERFILES = 40

try:
    from wheelgap import gapwheels
except ImportError:  # the pinned release may predate --fix
    gapwheels = None

# Targets of the last check(), by label, for fixes().
_targets: dict[str, Target] = {}
_last_check: tuple[list[Row], list[Target], Source] | None = None


def supports_gap_wheels() -> bool:
    return gapwheels is not None


def gap_wheels(list_json: str) -> str:
    """Reuse the CLI's eligibility rules and render the same installation instructions."""
    empty = {"cells": [], "text": "", "unofficial": ""}
    if gapwheels is None or _last_check is None:
        return json.dumps(empty)
    try:
        wheels = gapwheels.parse(json.loads(list_json))
    except ValueError:
        return json.dumps(empty)
    if not wheels:
        return json.dumps(empty)
    rows, targets, source = _last_check
    fills = gapwheels.fillable(
        rows,
        targets,
        {id(row.package): source for row in rows},
        wheels,
        origin.private_names([source]),
    )
    if not fills:
        return json.dumps(empty)
    return json.dumps(
        {
            "cells": gapwheels.to_json(fills, True),
            "text": "\n".join(gapwheels.text_lines(fills, True)).strip(),
            "unofficial": gapwheels.UNOFFICIAL,
        }
    )


def _skipped_folder(parts: tuple[str, ...]) -> bool:
    """A folder wheelgap --suggest-targets doesn't look in (hidden, node_modules, ...), or a vendored test fixture."""
    return any(p.startswith(".") or p in suggest_targets._SKIP_DIRS for p in parts)


def plan(paths_json: str) -> str:
    """The files to fetch, from the repo tree's paths (blobs only): lockfiles, workflows, Dockerfiles and the root
    .python-version. Lockfiles come shallowest first, then in wheelgap's order, so the first is the one
    `wheelgap` would pick in the repo's root (or the nearest folder that has one)."""
    paths = [p for p in json.loads(paths_json) if isinstance(p, str)]
    lockfiles, workflows, dockerfiles, templates = [], [], [], []
    for p in paths:
        pp = PurePosixPath(p)
        folders = pp.parts[:-1]
        if len(pp.parts) == 3 and folders == (".github", "workflows") and pp.suffix in (".yml", ".yaml"):
            workflows.append(p)
            continue
        if _skipped_folder(folders):
            continue
        if pp.name in LOCKFILES or pp.name == "pyproject.toml":
            lockfiles.append(p)
        kind = suggest_targets._container_kind(pp.name)
        if kind == "file":
            dockerfiles.append(p)
        elif kind == "template":
            templates.append(p)
    # A pyproject.toml comes after the lockfiles in the same folder, so a uv.lock next to one is still picked first.
    lockfiles.sort(
        key=lambda p: (
            p.count("/"),
            PurePosixPath(p).name not in LOCKFILES,
            LOCKFILES.index(PurePosixPath(p).name) if PurePosixPath(p).name in LOCKFILES else 0,
            p,
        )
    )
    dockerfiles.sort(key=lambda p: (p.count("/"), p))
    return json.dumps(
        {
            "lockfiles": [{"path": p, "offline": PurePosixPath(p).name in OFFLINE} for p in lockfiles[:MAX_LOCKFILES]],
            "workflows": sorted(workflows),
            "dockerfiles": dockerfiles[:MAX_DOCKERFILES],
            "more_dockerfiles": max(0, len(dockerfiles) - MAX_DOCKERFILES),
            "templates": sorted(templates),
            "python_version": ".python-version" if ".python-version" in paths else None,
        }
    )


def _python_version_file(root: Path) -> str | None:
    """The root .python-version's first version, as wheelgap reads it for its -p default (3.13, 3.14t)."""
    try:
        text = (root / ".python-version").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            parts = line.removeprefix("cpython-").removeprefix("python").split(".")
            if len(parts) >= 2 and parts[0] == "3" and parts[1].rstrip("t").isdigit():
                return f"3.{parts[1]}"
            return None
    return None


def detect(root: str) -> str:
    """wheelgap --suggest-targets on the fetched files, plus the defaults the page starts from when it finds none."""
    found = suggest_targets.suggest(Path(root))
    config = found.config()
    pythons = list(config.get("python") or [])
    python_from = "ci" if pythons else None
    if not pythons and (v := _python_version_file(Path(root))):
        pythons, python_from = [v], ".python-version"
    if not pythons:
        pythons, python_from = [f"{NEWEST_PYTHON[0]}.{NEWEST_PYTHON[1]}"], "default"
    platforms = list(config.get("targets") or [])
    return json.dumps(
        {
            "pythons": pythons,
            "python_from": python_from,
            "platforms": platforms or list(DEFAULT_PLATFORMS),
            "platforms_from": "ci" if platforms else "default",
            "glibc": config.get("glibc"),
            "musl": config.get("musl"),
            "macos": config.get("macos"),
            "found": [
                {"kind": kind, "value": f.value, "path": f.path, "line": f.line, "source": f.source}
                for kind, items in (("target", found.targets), ("python", found.pythons))
                for f in items
            ],
            "unreadable": [{"path": u.path, "line": u.line, "reason": u.reason} for u in found.unreadable],
        }
    )


def needs(root: str, path: str) -> str:
    """The PyPI lookups checking `path` needs: every package wheelgap's pypi.fill() would look up, with its
    canonical name and pinned version or null. Only names may_look_up() allows, so a package from another index
    (a requirements file's --index-url, a Pipfile.lock source) never reaches pypi.org and is skipped as
    `private index`, as the CLI does. Requirements includes (-r, -c) are read from disk, so the worker fetches
    them first; a missing one comes back as an error naming it, and the worker fetches it and retries.
    """
    try:
        source = load_file(Path(root) / path)
    except FileNotFoundError as e:  # an include (-r, -c) the worker hasn't fetched yet; name it
        inc = (e.filename or "").replace("\\", "/")
        repo = f"{root}".replace("\\", "/")
        if inc.startswith(f"{repo}/"):
            inc = inc[len(repo) + 1:]
        return json.dumps({"error": str(e).replace(f"{repo}/", ""), "kind": "include", "path": inc})
    except (SourceError, ValueError, OSError) as e:  # ValueError: a file that isn't valid TOML or JSON
        return json.dumps({"error": str(e).replace(f"{root}/", ""), "kind": "source"})
    lookups = []
    for pkg in source.packages:
        if pkg.skip:
            continue
        # A package from another index (a requirements file's --index-url, a Pipfile.lock source) is never
        # looked up on pypi.org and skipped as `private index`, as the CLI does; the name is internal.
        if (pkg.files is None or pkg.partial_files) and (
            not source.pypi_only or not pypi.may_look_up(pkg.registry)
        ):
            pkg.skip = "private index"
            continue
        if pkg.error or (pkg.files is not None and not pkg.partial_files):
            continue  # a parse error stays unknown; a package whose files are listed needs no lookup
        lookups.append({"name": canonicalize_name(pkg.name), "version": pkg.version or None})
    return json.dumps({"lookups": lookups, "total": len(lookups)})


def _fill_lookups(source, pythons: list[str], lookups: dict) -> None:
    """Fill each package that needs it from the PyPI project JSON the worker fetched, the way pypi.fill() does:
    a pinned package from its release's files, a range resolved per Python. A package that wasn't looked up
    (over the cap, failed, or 404) gets an error, so the check reports it as unknown, never as a gap."""
    base = pypi.index_url()
    for pkg in source.packages:
        if pkg.skip:
            continue
        if (pkg.files is None or pkg.partial_files) and (
            not source.pypi_only or not pypi.may_look_up(pkg.registry)
        ):
            pkg.skip = "private index"
            continue
        if pkg.files is not None and not pkg.partial_files:
            continue
        key = f"{canonicalize_name(pkg.name)}=={pkg.version}" if pkg.version else canonicalize_name(pkg.name)
        data = lookups.get(key)
        if not isinstance(data, dict):
            pkg.error = "not looked up"
            continue
        pypi._resolve_pythons(pkg, tuple(pythons), lambda p, py, d=data: pypi._fill_json_data(p, d, py, base), None)


def check(root: str, lockfile: str, options_json: str, lookups_json: str | None = None) -> str:
    """The lockfile's packages with a gap on any target, read offline from the files it lists.

    options: {"pythons": [...], "platforms": [...], "glibc"?, "musl"?, "macos"?}. A target Python the lock's
    requires-python leaves out is reported in `outside_lock` and none of its cells count as gaps: uv keeps no wheels
    for it in the lock, so they'd be false gaps (the CLI rechecks them on PyPI; the page says so instead).

    A file that doesn't list every package's files (LOOKUPS: Pipfile.lock, requirements files, pixi.lock,
    pyproject.toml) is checked from PyPI JSON the worker fetched for the lookups needs() named, keyed
    `name` (a range) or `name==version` (a pin). A package with no answer is reported as unknown.
    """
    global _last_check
    _last_check = None
    options = json.loads(options_json)
    limits = {k: options[k] for k in ("glibc", "musl", "macos") if isinstance(options.get(k), str)}
    try:
        targets = api.make_targets(options["pythons"], options["platforms"], **limits)
    except (TargetError, ValueError) as e:
        return json.dumps({"error": str(e), "kind": "targets"})
    path = Path(root) / lockfile
    try:
        source = load_file(path)
    except (SourceError, ValueError, OSError) as e:  # ValueError: a lockfile that isn't valid TOML or JSON
        return json.dumps({"error": str(e).replace(f"{root}/", ""), "kind": "source"})
    if path.name not in OFFLINE and source.kind not in ("requirements", "pip-report", "pyproject", "pipfile", "pixi"):
        return json.dumps({"error": f"{path.name} needs PyPI lookups", "kind": "needs-index"})
    outside = set(pythons_outside_lock(source, targets))
    if path.name in OFFLINE:
        try:
            # A listed lockfile can still leave a package's files out, so that reading it offline would need the
            # index; that's a "needs PyPI lookups" answer, not a lockfile the page failed to read.
            offline.require_offline([source])
        except SourceError as e:
            return json.dumps({"error": str(e), "kind": "needs-index"})
        try:
            results, source, check_rows = api._check_lockfile(path, targets, online=False, offline=True)
        except (SourceError, ValueError, OSError) as e:  # ValueError: a lockfile that isn't valid TOML or JSON
            return json.dumps({"error": str(e).replace(f"{root}/", ""), "kind": "source"})
        # --suggest's rule (origin.known_pypi): only packages positively from PyPI are looked up there, so a private
        # package's name never goes to pypi.org, and a same-named PyPI project is never offered as its fix.
        private = origin.private_names([source])
        on_pypi: dict[tuple[str, str | None], bool] = {}
        for original in source.packages:
            for p in original.variants:
                key = (canonicalize_name(p.name), p.version)
                on_pypi[key] = on_pypi.get(key, True) and origin.known_pypi(p, source, private=private)
    else:
        _fill_lookups(source, options["pythons"], json.loads(lookups_json) if lookups_json else {})
        # check.check() names its rows' per-target statuses `results`; the page reads the same shape as above.
        check_rows = list(api.check([source], targets))
        results = [api.Result(name=r.package.name, version=r.package.version, statuses=r.results) for r in check_rows]
        # Every package that was filled came from pypi.org's JSON API; ones skipped as a private index never
        # reach the rows below, so there's no namesake risk here.
        on_pypi = {}
        for pkg in source.packages:
            for p in (pkg, *pkg.resolutions.values()):
                if not p.error and p.version:
                    on_pypi[(canonicalize_name(p.name), p.version)] = True
    _targets.clear()
    _targets.update((t.label(True), t) for t in targets)
    counted = [t for t in targets if t.python not in outside]
    _last_check = (check_rows, counted, source)
    per_target = {t.label(True): {s.value: 0 for s in (Status.WHEEL, *GAPS, Status.UNKNOWN)} for t in targets}
    rows = []
    for r in results:
        statuses = {t.label(True): r.statuses.get(t, Status.NOT_NEEDED).value for t in targets}
        for t in counted:
            s = r.statuses.get(t)
            if s is not None and s.value in per_target[t.label(True)]:
                per_target[t.label(True)][s.value] += 1
        gaps = [t.label(True) for t in counted if r.statuses.get(t) in GAPS]
        unknown = [t.label(True) for t in counted if r.statuses.get(t) is Status.UNKNOWN]
        if gaps or unknown:
            keep = [t.label(True) for t in counted if r.statuses.get(t) is Status.WHEEL]
            lookup = bool(gaps and r.version) and on_pypi.get((canonicalize_name(r.name), r.version), False)
            rows.append(
                {
                    "name": r.name,
                    "version": r.version,
                    "statuses": statuses,
                    "gaps": gaps,
                    "unknown": unknown,
                    "keep": keep,
                    "lookup": lookup,
                }
            )
    return json.dumps(
        {
            "wheelgap": wheelgap.__version__,
            "lockfile": lockfile,
            "targets": [{"label": t.label(True), "python": t.python, "platform": t.platform} for t in targets],
            "requires_python": source.requires_python,
            "outside_lock": sorted(outside),
            "packages": len(results),
            "skipped": sorted({p.name for p in source.packages if p.skip}),
            "per_target": per_target,
            "rows": rows,
        }
    )


def fixes(releases_json: str, wanted_json: str) -> str:
    """For each wanted row ({name, version, gaps, keep} from check()), the release --suggest names for its gaps.

    releases: by name, the `releases` mapping of PyPI's project JSON. A name missing from it, or a row check() didn't
    mark for lookup (not positively a PyPI package), gets no answer: the page shows "not looked up", never "no newer
    release"."""
    releases, wanted = json.loads(releases_json), json.loads(wanted_json)
    out = {}
    for row in wanted:
        rel = releases.get(row["name"])
        if not row.get("lookup") or not row.get("gaps") or not isinstance(rel, dict) or not row.get("version"):
            continue
        gaps = [_targets[g] for g in row["gaps"] if g in _targets]
        keep = [_targets[k] for k in row["keep"] if k in _targets]
        try:
            found = release_suggest.suggest(rel, row["version"], gaps, keep)
        except ValueError:  # an invalid pinned version: nothing to compare against
            continue
        out[f"{row['name']}=={row['version']}"] = {
            "found": {t.label(True): v for t, v in found.found.items()},
            "loses": [t.label(True) for t in found.loses],
        }
    return json.dumps(out)
