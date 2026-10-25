#!/usr/bin/env python3
"""Génère catalog.json depuis models.dev : liste pré-filtrée pour les plugins Jeedom.

Usage : python scripts/build.py [--source api.json] [--out catalog.json] [--force]
Code de sortie : 0 = OK (fichier écrit ou inchangé), 2 = garde-fou déclenché, rien n'est écrit.
Bibliothèque standard uniquement.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_URL = "https://models.dev/api.json"
SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DROP_RATIO = 0.30  # garde-fou : part maximale de modèles qui peuvent disparaître d'un coup

ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
NON_CHAT_TYPES = {"embedding", "image", "video", "speech", "transcription", "reranking", "realtime"}
# Modèles spécialisés que models.dev ne marque pas toujours par « type ».
NAME_DENY = re.compile(
    r"(embed|whisper|tts|transcri|realtime|-live-|live-preview|computer-use|deep-research|customtools"
    r"|prompt-guard|safeguard|moderation|rerank|-image|image-|lyria|orpheus|voxtral|multi-agent"
    r"|audio|-exp$|-exp-)",
    re.I,
)
# Snapshot daté ou numéroté en fin d'id : -20251101, -2024-11-20, -2603.
SNAPSHOT_RE = re.compile(r"^(?P<base>.+?)-(?:\d{8}|\d{4}-\d{2}-\d{2}|\d{4})$")
INPUT_KINDS = {"text", "image", "pdf", "audio", "video"}
PATCHABLE = {"name", "label", "family", "release_date", "status", "context", "output", "input",
             "tools", "reasoning", "structured_output", "cost", "free"}


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fetch_source(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-models-catalog (github.com/limad/ai-models-catalog)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read(MAX_SOURCE_BYTES + 1)
    if len(body) > MAX_SOURCE_BYTES:
        raise RuntimeError("source trop volumineuse")
    return json.loads(body)


def reject_reason(mid, m, cfg):
    """Raison d'exclusion d'un modèle models.dev, ou None s'il est gardé."""
    if m.get("type") in NON_CHAT_TYPES:
        return "type:" + m["type"]
    if "text" not in (m.get("modalities", {}).get("output") or []):
        return "sortie non texte"
    if m.get("status") == "deprecated":
        return "obsolète"
    if cfg.get("require_tools", True) and not m.get("tool_call"):
        return "sans outils"
    if NAME_DENY.search(mid):
        return "spécialisé"
    vendors = cfg.get("vendors")
    if vendors and mid.split("/", 1)[0] not in vendors:
        return "fournisseur d'origine non retenu"
    if mid.startswith("~"):
        return "alias virtuel"
    return None


def group_key(mid, m, cfg):
    family = m.get("family") or mid
    if cfg.get("vendors"):
        return mid.split("/", 1)[0] + "/" + family
    return family


def dedupe(kept, pins, dropped):
    """Retire les snapshots et les alias « -latest » qui doublonnent un autre id gardé."""
    for mid in sorted(kept):
        if mid in pins:
            continue
        m = kept[mid]
        snap = SNAPSHOT_RE.match(mid)
        if snap:
            base = snap.group("base")
            for alias in (base, base + "-latest"):
                a = kept.get(alias)
                if a is not None:
                    # L'alias suit la dernière version : il prend la date du snapshot si elle est plus récente.
                    a["_date"] = max(a["_date"], m["_date"])
                    dropped[mid] = "snapshot de " + alias
                    break
            if mid in dropped:
                continue
        if mid.endswith("-latest"):
            for oid, o in kept.items():
                if (oid != mid and not oid.endswith("-latest") and oid not in dropped
                        and o.get("family") == m.get("family")
                        and o["_date"] == m["_date"]):
                    dropped[mid] = "alias de " + oid
                    break
    for mid in dropped:
        kept.pop(mid, None)


def keep_recent(kept, pins, cfg, per_family, min_date, dropped):
    """Par famille, garde les modèles des N dates de sortie les plus récentes, et pas plus vieux que min_date."""
    groups = {}
    for mid, m in kept.items():
        groups.setdefault(group_key(mid, m, cfg), []).append(mid)
    for ids in groups.values():
        dates = sorted({kept[i]["_date"] for i in ids}, reverse=True)[:per_family]
        for i in ids:
            if i in pins:
                continue
            if kept[i]["_date"] not in dates:
                dropped[i] = "ancienne version de la famille"
            elif kept[i]["_date"] and kept[i]["_date"] < min_date:
                dropped[i] = "trop ancien"
    for mid in dropped:
        kept.pop(mid, None)


def to_entry(m, origin):
    limit = m.get("limit") or {}
    entry = {
        "name": m.get("name") or m.get("id"),
        "status": m.get("status") if m.get("status") in ("beta", "deprecated") else "active",
        "context": int(limit.get("context") or 0),
        "input": [k for k in (m.get("modalities", {}).get("input") or ["text"]) if k in INPUT_KINDS],
        "tools": bool(m.get("tool_call")),
        "reasoning": bool(m.get("reasoning")),
        "origin": origin,
    }
    if m.get("family"):
        entry["family"] = m["family"]
    if m.get("release_date"):
        entry["release_date"] = m["release_date"]
    if limit.get("output"):
        entry["output"] = int(limit["output"])
    if "structured_output" in m:
        entry["structured_output"] = bool(m["structured_output"])
    cost = m.get("cost") or {}
    c = {dst: cost[src] for src, dst in (("input", "in"), ("output", "out"),
                                         ("cache_read", "cache_read"), ("cache_write", "cache_write"))
         if isinstance(cost.get(src), (int, float)) and cost[src] >= 0}
    if c:
        entry["cost"] = c
        if c.get("in") == 0 and c.get("out") == 0:
            entry["free"] = True
    return entry


def apply_patch(entry, patch):
    for k, v in patch.items():
        if k in PATCHABLE:
            entry[k] = v
    return entry


def build_provider(pid, cfg, ov, source, defaults, report):
    src = source.get(cfg["source"])
    if not src:
        raise RuntimeError(f"{pid} : fournisseur models.dev « {cfg['source']} » introuvable")
    models = src.get("models") or {}
    pins = set(ov.get("pin", []))
    excludes = ov.get("exclude", [])
    patches = ov.get("models", {})
    kept, dropped = {}, {}

    for mid, m in models.items():
        if not ID_RE.match(mid):
            dropped[mid] = "id invalide"
        elif any(fnmatch.fnmatchcase(mid, p) for p in excludes):
            dropped[mid] = "exclu (overrides)"
        elif mid not in pins and (reason := reject_reason(mid, m, cfg)):
            dropped[mid] = reason
        else:
            kept[mid] = dict(m, _date=m.get("release_date") or "")
    dedupe(kept, pins, dropped)
    keep_recent(kept, pins, cfg, int(cfg.get("per_family", defaults["per_family"])),
                cfg.get("min_date", defaults["min_date"]), dropped)

    out = {mid: to_entry(m, "models.dev") for mid, m in kept.items()}
    sort_date = {mid: m["_date"] for mid, m in kept.items()}
    for mid in sorted(pins - set(out)):
        patch = patches.get(mid, {})
        if mid in models or not (patch.get("name") and patch.get("context")) or not ID_RE.match(mid):
            report["warnings"].append(f"{pid}/{mid} : épinglé mais introuvable et sans données suffisantes")
            continue
        out[mid] = {"name": patch["name"], "status": "active", "context": int(patch["context"]),
                    "input": ["text"], "tools": True, "reasoning": False, "origin": "override"}
    for mid, entry in out.items():
        apply_patch(entry, patches.get(mid, {}))
        if mid in pins:
            entry["recommended"] = True

    ordered = dict(sorted(out.items(), key=lambda kv: (sort_date.get(kv[0], kv[1].get("release_date") or ""), kv[0]),
                          reverse=True))
    prov = {"label": cfg["label"], "source_id": cfg["source"]}
    if ov.get("default_model"):
        prov["default_model"] = ov["default_model"]
    if "free_tier" in ov:
        prov["free_tier"] = bool(ov["free_tier"])
    prov["models"] = ordered
    report["providers"][pid] = {"source": len(models), "kept": len(ordered), "dropped": dropped}
    return prov


def validate(catalog):
    """Contrôles minimaux en plus du schéma JSON (lancé séparément en CI)."""
    errors = []
    for pid, p in catalog["providers"].items():
        if not p["models"]:
            errors.append(f"{pid} : aucun modèle")
        dm = p.get("default_model")
        if dm and dm not in p["models"]:
            errors.append(f"{pid} : default_model « {dm} » absent de la liste")
        for mid in p["models"]:
            if not ID_RE.match(mid):
                errors.append(f"{pid} : id invalide {mid!r}")
    return errors


def guard(previous, catalog):
    """Bloque une régression brutale (panne ou erreur côté models.dev)."""
    if not previous:
        return []
    errors = []
    prev_ids = {(p, m) for p, pv in previous.get("providers", {}).items() for m in pv.get("models", {})}
    new_ids = {(p, m) for p, pv in catalog["providers"].items() for m in pv["models"]}
    for pid in previous.get("providers", {}):
        if pid not in catalog["providers"]:
            errors.append(f"fournisseur disparu : {pid}")
    lost = prev_ids - new_ids
    if prev_ids and len(lost) / len(prev_ids) > MAX_DROP_RATIO:
        errors.append(f"{len(lost)}/{len(prev_ids)} modèles disparaîtraient (> {int(MAX_DROP_RATIO * 100)} %)")
    return errors


def write_summary(report, lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", help="fichier api.json local (sinon téléchargé depuis models.dev)")
    ap.add_argument("--out", default=str(ROOT / "catalog.json"))
    ap.add_argument("--force", action="store_true", help="ignore le garde-fou de régression")
    ap.add_argument("--report", action="store_true", help="affiche le détail des modèles exclus")
    args = ap.parse_args()

    source = load_json(args.source) if args.source else fetch_source(SOURCE_URL)
    providers_cfg = load_json(ROOT / "config" / "providers.json")
    overrides = load_json(ROOT / "config" / "overrides.json").get("providers", {})
    cfg_defaults = providers_cfg.get("defaults", {})
    today = datetime.now(timezone.utc).date()
    months = int(cfg_defaults.get("max_age_months", 18))
    y, mo = divmod(today.year * 12 + today.month - 1 - months, 12)
    defaults = {"per_family": int(cfg_defaults.get("per_family", 2)), "min_date": f"{y:04d}-{mo + 1:02d}-{today.day:02d}"}

    report = {"providers": {}, "warnings": []}
    providers = {pid: build_provider(pid, cfg, overrides.get(pid, {}), source, defaults, report)
                 for pid, cfg in providers_cfg["providers"].items()}

    out_path = Path(args.out)
    previous = load_json(out_path) if out_path.exists() else None
    unchanged = previous is not None and previous.get("providers") == providers
    catalog = {
        "schema": SCHEMA_VERSION,
        "generated_at": previous["generated_at"] if unchanged
        else datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {"name": "models.dev", "url": SOURCE_URL, "license": "MIT"},
        "providers": providers,
    }

    lines = ["| Fournisseur | models.dev | Gardés |", "|---|---|---|"]
    for pid, r in report["providers"].items():
        lines.append(f"| {pid} | {r['source']} | {r['kept']} |")
    total = sum(len(p["models"]) for p in providers.values())
    lines.append(f"\nTotal : {total} modèles")
    lines += [f"- ⚠️ {w}" for w in report["warnings"]]
    print("\n".join(lines))
    if args.report:
        for pid, r in report["providers"].items():
            print(f"\n== {pid} : exclus")
            for mid, why in sorted(r["dropped"].items()):
                print(f"  {mid:55} {why}")

    errors = validate(catalog) + ([] if args.force else guard(previous, catalog))
    if errors:
        print("\nÉCHEC, catalog.json non modifié :", *errors, sep="\n- ", file=sys.stderr)
        write_summary(report, lines + ["", "**Échec :**"] + [f"- {e}" for e in errors])
        return 2
    write_summary(report, lines)

    if unchanged:
        print("\nAucun changement.")
        return 0
    data = (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_bytes(data)
    tmp.replace(out_path)
    digest = hashlib.sha256(data).hexdigest()
    out_path.with_name(out_path.name + ".sha256").write_text(f"{digest}  {out_path.name}\n", encoding="utf-8")
    print(f"\nÉcrit : {out_path} ({len(data)} octets, sha256 {digest[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
