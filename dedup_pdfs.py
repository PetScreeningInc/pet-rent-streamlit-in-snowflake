#!/usr/bin/env python3
"""
dedup_pdfs.py — Delete older duplicate PDFs from the batch output folder.

Same dedup logic as extract_pdf_summary.py:
  - Group PDFs by (prop_id, parent_id) parsed from filename
  - For each group, keep the PDF with the latest run_date (YYYYMMDD)
  - Move older duplicates to a 'duplicates' subfolder (or delete with --delete)

Default is SAFE (move, don't delete) so you can recover if the script
gets it wrong.

Usage:
    python dedup_pdfs.py batch_pdfs/             # dry run, show what would happen
    python dedup_pdfs.py batch_pdfs/ --apply     # move to batch_pdfs/duplicates/
    python dedup_pdfs.py batch_pdfs/ --apply --delete   # actually delete (no recovery)
"""
import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


# ─── Filename parser (same as extract_pdf_summary.py) ───────────────
def parse_filename(name: str) -> Dict[str, str]:
    stem = Path(name).stem
    parts = stem.split("_")
    info: Dict[str, str] = {"raw_stem": stem}

    if not parts:
        return info

    if re.match(r"^\d{8}$", parts[-1]):
        info["run_date"] = parts[-1]
        body = parts[:-1]
    else:
        body = parts

    nums = [p for p in body if p.isdigit() and len(p) >= 2]
    if len(nums) >= 2:
        info["prop_id"]   = nums[0]
        info["parent_id"] = nums[-1]
    elif len(nums) == 1:
        info["parent_id"] = nums[0]
        info["prop_id"]   = nums[0]

    return info


def dedup_key(info: Dict) -> str:
    pid    = info.get("prop_id", "")
    parent = info.get("parent_id", "")

    if pid and parent and pid != parent:
        return f"pp_{pid}_{parent}"
    if parent:
        return f"id_{parent}"
    if pid:
        return f"id_{pid}"

    stem = info.get("raw_stem", "")
    return f"stem_{re.sub(r'_\\d{8}$', '', stem)}"


# ─── Main ──────────────────────────────────────────────────────────
def main():
    pa = argparse.ArgumentParser(description="Delete older duplicate PDFs from batch output.")
    pa.add_argument("folder", help="Folder containing PDFs (searches recursively)")
    pa.add_argument("--apply", action="store_true",
                    help="Actually move/delete (default = dry run, just shows what would happen)")
    pa.add_argument("--delete", action="store_true",
                    help="Permanently delete instead of moving to duplicates/. NO RECOVERY.")
    pa.add_argument("--dest", default="duplicates",
                    help="Subfolder to move duplicates into (default: duplicates/)")
    args = pa.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"❌ {folder} is not a directory")

    # Collect all PDFs grouped by dedup_key
    pdfs = sorted(folder.rglob("*.pdf"))
    if not pdfs:
        sys.exit(f"❌ No PDFs found in {folder}")

    # Skip the dedup destination folder itself
    dest_dir = folder / args.dest
    pdfs = [p for p in pdfs if dest_dir not in p.parents]

    groups: Dict[str, List[Tuple[Path, str]]] = defaultdict(list)
    for p in pdfs:
        info = parse_filename(p.name)
        key = dedup_key(info)
        date = info.get("run_date", "")
        groups[key].append((p, date))

    # For each group with > 1, the keeper is the one with the largest date
    keepers: List[Path] = []
    losers:  List[Path] = []
    for key, items in groups.items():
        if len(items) == 1:
            keepers.append(items[0][0])
            continue
        # Sort by date desc — newest first
        items.sort(key=lambda t: t[1] or "", reverse=True)
        keepers.append(items[0][0])
        for p, _ in items[1:]:
            losers.append(p)

    print(f"Scanned {len(pdfs)} PDFs in {folder}")
    print(f"Unique reports: {len(groups)}")
    print(f"Keepers:        {len(keepers)}")
    print(f"Older dupes:    {len(losers)}")
    print("-" * 80)

    if not losers:
        print("✓ No duplicates found. Nothing to do.")
        return

    # Print what would happen
    for key, items in groups.items():
        if len(items) == 1:
            continue
        items.sort(key=lambda t: t[1] or "", reverse=True)
        keep_p, keep_d = items[0]
        print(f"\n{key}")
        print(f"  KEEP   {keep_d}  {keep_p.relative_to(folder)}")
        for p, d in items[1:]:
            verb = "DELETE" if args.delete else "MOVE  "
            print(f"  {verb} {d}  {p.relative_to(folder)}")

    if not args.apply:
        print("\n(dry run — no files touched. Add --apply to execute.)")
        return

    # Execute
    if args.delete:
        print(f"\nDeleting {len(losers)} older PDFs...")
        for p in losers:
            try:
                p.unlink()
            except Exception as e:
                print(f"  ⚠ Could not delete {p}: {e}")
        print(f"✓ Deleted {len(losers)} files")
    else:
        dest_dir.mkdir(exist_ok=True)
        print(f"\nMoving {len(losers)} older PDFs to {dest_dir}/ ...")
        moved = 0
        for p in losers:
            try:
                # Preserve subfolder structure under dest
                rel_parent = p.relative_to(folder).parent
                target_dir = dest_dir / rel_parent
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / p.name
                # Avoid overwriting if dest already has a file with same name
                if target.exists():
                    target = target_dir / f"{target.stem}_orig{target.suffix}"
                shutil.move(str(p), str(target))
                moved += 1
            except Exception as e:
                print(f"  ⚠ Could not move {p}: {e}")
        print(f"✓ Moved {moved} files to {dest_dir}/")
        print(f"  (delete that folder when you're sure: rm -rf {dest_dir})")


if __name__ == "__main__":
    main()
