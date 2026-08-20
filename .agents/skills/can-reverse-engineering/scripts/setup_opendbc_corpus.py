#!/usr/bin/env python3
"""Build an organized, symlink-only corpus from a local opendbc checkout.

The source checkout is never changed unless ``--generate`` is supplied.  In
that case this helper invokes opendbc's official DBC generator before taking
the source inventory.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Iterable


MANIFEST_FIELDS = (
    "dbc_filename",
    "original_source_path",
    "corpus_symlink_path",
    "oem_family",
    "category",
    "origin_type",
)

# The directory names preserve the organization of the validated corpus while
# the manifest retains the human-readable family name.
FAMILY_DIRECTORIES = {
    "BMW": "BMW",
    "Chrysler/Stellantis": "Chrysler-Stellantis",
    "Comma": "Comma",
    "Ford": "Ford",
    "GM": "GM",
    "Great Wall Motor": "Great-Wall-Motor",
    "Honda/Acura": "Honda-Acura",
    "Hongqi": "Hongqi",
    "Hyundai/Kia": "Hyundai-Kia",
    "Luxgen": "Luxgen",
    "MG": "MG",
    "Mazda": "Mazda",
    "Mercedes-Benz": "Mercedes-Benz",
    "Nissan": "Nissan",
    "Opel": "Opel",
    "PSA": "PSA",
    "Rivian": "Rivian",
    "Subaru": "Subaru",
    "Tesla": "Tesla",
    "Toyota/Lexus": "Toyota-Lexus",
    "Unclassified": "Unclassified",
    "Volkswagen": "Volkswagen",
    "Volvo": "Volvo",
}

# Prefixes are deliberately limited to unambiguous OEM/brand names represented
# by the current corpus.  A new, unclear filename falls through to Unclassified.
FAMILY_PREFIXES = (
    (("acura_", "honda_"), "Honda/Acura"),
    (("bmw_",), "BMW"),
    (("cadillac_", "gm_"), "GM"),
    (("chrysler_", "fca_", "stellantis_"), "Chrysler/Stellantis"),
    (("comma_",), "Comma"),
    (("ford_", "lincoln_"), "Ford"),
    (("gwm_",), "Great Wall Motor"),
    (("hongqi_",), "Hongqi"),
    (("hyundai_", "kia_"), "Hyundai/Kia"),
    (("luxgen_",), "Luxgen"),
    (("mazda_",), "Mazda"),
    (("mercedes_benz_",), "Mercedes-Benz"),
    (("mg.dbc", "mg_"), "MG"),
    (("nissan_",), "Nissan"),
    (("opel_",), "Opel"),
    (("psa_",), "PSA"),
    (("rivian_",), "Rivian"),
    (("subaru_",), "Subaru"),
    (("tesla_",), "Tesla"),
    (("toyota_", "lexus_"), "Toyota/Lexus"),
    (("volvo_",), "Volvo"),
    (("vw_",), "Volkswagen"),
)

# These names do not carry a category keyword.  Keeping the validated current
# set explicit avoids guessing that every future model-named DBC is a platform.
KNOWN_PLATFORM_SPECIFIC = frozenset(
    {
        "acura_ilx_2016_nidec.dbc",
        "bmw_e9x_e8x.dbc",
        "chrysler_cusw.dbc",
        "chrysler_pacifica_2017_hybrid_generated.dbc",
        "chrysler_pacifica_2017_hybrid_private_fusion.dbc",
        "chrysler_ram_dt_generated.dbc",
        "chrysler_ram_hd_generated.dbc",
        "fca_giorgio.dbc",
        "gwm_haval_h6_phev_2024.dbc",
        "honda_bosch_radarless_generated.dbc",
        "honda_odyssey_exl_2018_generated.dbc",
        "honda_odyssey_twn_2018_generated.dbc",
        "hongqi_hs5.dbc",
        "hyundai_palisade_2023_generated.dbc",
        "luxgen_s5_2015.dbc",
        "mazda_2017.dbc",
        "mazda_3_2019.dbc",
        "mazda_rx8.dbc",
        "mercedes_benz_e350_2010.dbc",
        "mg.dbc",
        "nissan_leaf_2018_generated.dbc",
        "nissan_x_trail_2017_generated.dbc",
        "nissan_xterra_2011.dbc",
        "opel_omega_2001.dbc",
        "psa_aee2010_r3.dbc",
        "subaru_forester_2017_generated.dbc",
        "subaru_global_2017_generated.dbc",
        "subaru_global_2020_hybrid_generated.dbc",
        "subaru_outback_2015_generated.dbc",
        "subaru_outback_2019_generated.dbc",
        "tesla_model3_party.dbc",
        "tesla_model3_vehicle.dbc",
        "vw_meb_2024_generated.dbc",
        "vw_meb_generated.dbc",
        "vw_mlb.dbc",
        "vw_mqb.dbc",
        "vw_mqbevo.dbc",
        "vw_pq.dbc",
    }
)
KNOWN_LEGACY = frozenset(
    {
        "hyundai_2015_ccan.dbc",
        "hyundai_2015_mcan.dbc",
        "hyundai_i30_2014.dbc",
        "hyundai_santafe_2007.dbc",
    }
)

_PT_TOKEN = re.compile(r"(?:^|_)pt(?:_|\.dbc$)")
_CAN_TOKEN = re.compile(r"(?:^|_)can(?:_|\.dbc$)")
_INVENTORY_MARKER = "OPENDDBC_GENERATED_OUTPUTS="


class SetupError(RuntimeError):
    """A user-facing setup or safety error."""


def _generator_environment(opendbc_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(opendbc_root) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    return environment


def _run_generator(opendbc_root: Path, generator: Path) -> None:
    print(f"Running official opendbc generator: {generator}", flush=True)
    completed = subprocess.run(
        [sys.executable, str(generator)],
        cwd=opendbc_root,
        env=_generator_environment(opendbc_root),
        check=False,
    )
    if completed.returncode:
        raise SetupError(
            f"official opendbc generator failed with exit code {completed.returncode}"
        )


def _expected_generated_dbcs(opendbc_root: Path, generator: Path) -> set[str]:
    """Ask the official generator for its output names without writing them."""

    inventory_code = r'''
import json
import runpy
import sys

namespace = runpy.run_path(sys.argv[1], run_name="opendbc_corpus_inventory")
generate_all = namespace.get("generate_all")
if generate_all is None:
    raise RuntimeError("official generator does not expose generate_all()")
names = []
for name in generate_all():
    names.append(name if name.endswith(".dbc") else name + ".dbc")
print("OPENDDBC_GENERATED_OUTPUTS=" + json.dumps(sorted(names)))
'''
    completed = subprocess.run(
        [sys.executable, "-c", inventory_code, str(generator)],
        cwd=opendbc_root,
        env=_generator_environment(opendbc_root),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise SetupError(
            "could not inventory the official generator outputs"
            + (f": {detail}" if detail else "")
        )
    marker_lines = [
        line for line in completed.stdout.splitlines() if line.startswith(_INVENTORY_MARKER)
    ]
    if len(marker_lines) != 1:
        raise SetupError("official generator did not return a usable output inventory")
    try:
        names = json.loads(marker_lines[0][len(_INVENTORY_MARKER) :])
    except json.JSONDecodeError as exc:
        raise SetupError(f"invalid output inventory from official generator: {exc}") from exc
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise SetupError("invalid output names returned by official generator")
    if len(names) != len(set(names)):
        raise SetupError("official generator returned duplicate output names")
    return set(names)


def _source_inventory(dbc_dir: Path, expected_generated: set[str]) -> list[Path]:
    sources = sorted(
        (
            path.resolve(strict=True)
            for path in dbc_dir.iterdir()
            if path.is_file() and path.suffix == ".dbc" and not path.name.startswith("_")
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )
    missing = sorted(expected_generated - {path.name for path in sources})
    if missing:
        formatted = "\n  ".join(missing)
        raise SetupError(
            "official generated DBCs are missing from "
            f"{dbc_dir}:\n  {formatted}\n"
            "Re-run with --generate to invoke opendbc/dbc/generator/generator.py."
        )
    if not sources:
        raise SetupError(f"no final top-level DBC files found in {dbc_dir}")
    names = [path.name for path in sources]
    resolved = [str(path) for path in sources]
    if len(names) != len(set(names)) or len(resolved) != len(set(resolved)):
        raise SetupError("source inventory contains duplicate DBCs")
    return sources


def _family_for(filename: str) -> str | None:
    folded = filename.casefold()
    for prefixes, family in FAMILY_PREFIXES:
        if folded.startswith(prefixes):
            return family
    return None


def _category_for(filename: str) -> str | None:
    folded = filename.casefold()
    if filename in {"ESR.dbc", "FORD_CADS.dbc", "FORD_CADS_64.dbc"}:
        return "radar"
    if "park_assist" in folded:
        return "park-assist"
    if "actuator" in folded:
        return "actuator"
    if "radar" in folded and "radarless" not in folded:
        return "radar"
    if "adas" in folded:
        return "ADAS"
    if "body" in folded:
        return "body"
    if "chassis" in folded:
        return "chassis"
    if "object" in folded:
        return "object"
    if "high_voltage" in folded:
        return "high-voltage"
    if "lowspeed" in folded or "low_speed" in folded:
        return "low-speed"
    if "powertrain" in folded or "ptcan" in folded or _PT_TOKEN.search(folded):
        return "powertrain"
    if "canfd" in folded or "can_fd" in folded:
        return "CAN-FD"
    if _CAN_TOKEN.search(folded):
        return "CAN"
    if filename in KNOWN_LEGACY:
        return "legacy"
    if filename in KNOWN_PLATFORM_SPECIFIC:
        return "platform-specific"
    return None


def classify(filename: str) -> tuple[str, str]:
    """Return a conservative deterministic (family, category) classification."""

    family = _family_for(filename)
    category = _category_for(filename)
    if family is None:
        return "Unclassified", category or "Unclassified"
    if category is None:
        return "Unclassified", "Unclassified"
    return family, category


def _manifest_rows(sources: Iterable[Path], corpus_out: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source in sources:
        family, category = classify(source.name)
        destination = (
            corpus_out / FAMILY_DIRECTORIES[family] / category / source.name
        )
        rows.append(
            {
                "dbc_filename": source.name,
                "original_source_path": str(source),
                "corpus_symlink_path": str(destination),
                "oem_family": family,
                "category": category,
                "origin_type": (
                    "generated" if source.name.endswith("_generated.dbc") else "direct"
                ),
            }
        )
    return rows


def _load_existing_manifest(corpus_out: Path) -> list[dict[str, str]] | None:
    manifest = corpus_out / "manifest.tsv"
    if not manifest.exists():
        if corpus_out.exists() and any(corpus_out.iterdir()):
            raise SetupError(
                f"refusing non-empty --corpus-out without a compatible manifest: {corpus_out}"
            )
        return None
    if not manifest.is_file() or manifest.is_symlink():
        raise SetupError(f"existing manifest is not a regular file: {manifest}")
    with manifest.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
            raise SetupError(
                f"existing manifest has incompatible columns: {reader.fieldnames}"
            )
        return [dict(row) for row in reader]


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _path_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _owned_paths(
    existing_rows: list[dict[str, str]] | None, corpus_out: Path
) -> set[Path]:
    owned: set[Path] = set()
    for row in existing_rows or []:
        raw = row.get("corpus_symlink_path", "")
        path = Path(os.path.abspath(os.path.expanduser(raw)))
        if not _path_below(path, corpus_out):
            raise SetupError(f"existing manifest records a path outside the corpus: {raw}")
        if path in owned:
            raise SetupError(f"existing manifest duplicates corpus path: {path}")
        owned.add(path)
    return owned


def _check_parent_collisions(destination: Path, corpus_out: Path) -> None:
    parent = destination.parent
    while parent != corpus_out:
        if parent.is_symlink():
            raise SetupError(f"refusing corpus path below a symlinked directory: {parent}")
        if _lexists(parent) and not parent.is_dir():
            raise SetupError(f"corpus directory path is occupied by a file: {parent}")
        parent = parent.parent


def _write_manifest_atomic(
    manifest: Path, rows: list[dict[str, str]]
) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            prefix=".manifest.tsv.",
            dir=manifest.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer = csv.DictWriter(
                handle,
                fieldnames=MANIFEST_FIELDS,
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, manifest)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _build_corpus(corpus_out: Path, rows: list[dict[str, str]]) -> None:
    existing_rows = _load_existing_manifest(corpus_out)
    owned = _owned_paths(existing_rows, corpus_out)
    desired = {Path(row["corpus_symlink_path"]): row for row in rows}
    if len(desired) != len(rows):
        raise SetupError("classification produced duplicate corpus destinations")

    for destination, row in desired.items():
        _check_parent_collisions(destination, corpus_out)
        source = Path(row["original_source_path"])
        if not _lexists(destination):
            continue
        if not destination.is_symlink():
            raise SetupError(f"refusing to overwrite non-symlink corpus entry: {destination}")
        if destination.resolve(strict=False) == source:
            continue
        if destination not in owned:
            raise SetupError(f"refusing to replace an unmanaged symlink: {destination}")

    stale = owned - set(desired)
    for path in stale:
        _check_parent_collisions(path, corpus_out)
        if _lexists(path) and not path.is_symlink():
            raise SetupError(f"refusing to remove a stale non-symlink corpus entry: {path}")

    corpus_out.mkdir(parents=True, exist_ok=True)
    changes: list[tuple[Path, str | None]] = []
    try:
        for destination, row in desired.items():
            source = Path(row["original_source_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink() and destination.resolve(strict=False) == source:
                continue
            old_target = os.readlink(destination) if destination.is_symlink() else None
            if destination.is_symlink():
                destination.unlink()
            destination.symlink_to(source)
            changes.append((destination, old_target))

        for path in sorted(stale, key=str):
            if path.is_symlink():
                old_target = os.readlink(path)
                path.unlink()
                changes.append((path, old_target))

        for destination, row in desired.items():
            source = Path(row["original_source_path"])
            if not destination.is_symlink() or not destination.exists():
                raise SetupError(f"created corpus entry is not a usable symlink: {destination}")
            if destination.resolve(strict=True) != source:
                raise SetupError(f"created symlink points at the wrong source: {destination}")

        _write_manifest_atomic(corpus_out / "manifest.tsv", rows)
    except Exception:
        for path, old_target in reversed(changes):
            if path.is_symlink():
                path.unlink()
            if old_target is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(old_target)
        raise


def _validate_paths(opendbc_root: Path, corpus_out: Path) -> tuple[Path, Path]:
    try:
        root = opendbc_root.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise SetupError(f"--opendbc-root does not exist: {opendbc_root}") from exc
    if not root.is_dir():
        raise SetupError(f"--opendbc-root is not a directory: {root}")
    dbc_dir = root / "opendbc" / "dbc"
    generator = dbc_dir / "generator" / "generator.py"
    if not dbc_dir.is_dir():
        raise SetupError(f"opendbc DBC directory not found: {dbc_dir}")
    if not generator.is_file():
        raise SetupError(f"official opendbc generator not found: {generator}")

    output = corpus_out.expanduser().resolve(strict=False)
    if output == root or _path_below(output, root):
        raise SetupError("--corpus-out must be outside the opendbc checkout")
    return root, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opendbc-root",
        type=Path,
        required=True,
        help="local opendbc checkout containing opendbc/dbc/",
    )
    parser.add_argument(
        "--corpus-out",
        type=Path,
        required=True,
        help="external directory to create or safely update",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="run opendbc's official DBC generator before building the corpus",
    )
    args = parser.parse_args()

    try:
        opendbc_root, corpus_out = _validate_paths(args.opendbc_root, args.corpus_out)
        dbc_dir = opendbc_root / "opendbc" / "dbc"
        generator = dbc_dir / "generator" / "generator.py"
        if args.generate:
            _run_generator(opendbc_root, generator)
            try:
                expected_generated = _expected_generated_dbcs(opendbc_root, generator)
            except SetupError:
                # Older official generators may not expose the current read-only
                # generate_all() API. A successful explicit generator run is the
                # authoritative completeness check for those checkouts.
                expected_generated = {
                    path.name for path in dbc_dir.glob("*_generated.dbc")
                }
        else:
            try:
                expected_generated = _expected_generated_dbcs(opendbc_root, generator)
            except SetupError as exc:
                raise SetupError(
                    f"{exc}. Re-run with --generate so the official generator can "
                    "prepare a complete set."
                ) from exc
        sources = _source_inventory(dbc_dir, expected_generated)
        rows = _manifest_rows(sources, corpus_out)
        _build_corpus(corpus_out, rows)
    except (OSError, SetupError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    origins = Counter(row["origin_type"] for row in rows)
    unclassified = [
        row["dbc_filename"] for row in rows if row["oem_family"] == "Unclassified"
    ]
    print(f"Built opendbc corpus at {corpus_out}")
    print(
        f"Selected {len(rows)} final DBCs: {origins['direct']} direct, "
        f"{origins['generated']} generated"
    )
    print(f"Manifest: {corpus_out / 'manifest.tsv'}")
    print(
        "Unclassified: " + (", ".join(unclassified) if unclassified else "none")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
