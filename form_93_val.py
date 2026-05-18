"""
Form 93 PDF → JSON Income Tax Validation Pipeline
==================================================
Validates Income Tax values extracted from a Form 93 PDF
against a corresponding JSON data file, matched by voucher number.

Usage:
    python form93_validate.py data\
    python form93_validate.py --pdf form_93.pdf --json data.json
    python form93_validate.py --pdf form_93.pdf --json data.json --out results.csv

Requirements:
    pip install pdfplumber
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pdfplumber")

try:
    import fitz as pymupdf          # pymupdf fallback for corrupt/non-standard PDFs
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


# ---------------------------------------------------------------------------
# File health check
# ---------------------------------------------------------------------------

def _check_pdf_file(pdf_path: str):
    """Validate the PDF file before attempting to open it."""
    path = Path(pdf_path)

    size = path.stat().st_size
    if size == 0:
        sys.exit(
            f"Error: '{path.name}' is an empty file (0 bytes).\n"
            "The file was not saved correctly. Please re-download or re-copy it and try again."
        )

    with open(pdf_path, "rb") as f:
        header = f.read(5)
    if header != b"%PDF-":
        sys.exit(
            f"Error: '{path.name}' does not look like a valid PDF (missing %PDF header).\n"
            f"File size: {size} bytes. First bytes: {header}\n"
            "It may have been saved incorrectly (e.g. as an HTML error page)."
        )


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _extract_text_pdfplumber(pdf_path: str) -> str:
    """Extract full text using pdfplumber."""
    all_text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            all_text += (page.extract_text() or "") + "\n"
    return all_text


def _extract_text_pymupdf(pdf_path: str) -> str:
    """Extract full text using pymupdf (fallback for corrupt/non-standard PDFs)."""
    all_text = ""
    doc = pymupdf.open(pdf_path)
    for page in doc:
        all_text += page.get_text() + "\n"
    doc.close()
    return all_text


def extract_pdf_records(pdf_path: str) -> dict:
    """
    Extract voucher -> income_tax from a Form 93 PDF.

    Pattern matched per line:
        <voucher_number>  <DD/MM/YYYY>  <gross_amount>  <income_tax>  ...

    Tries pdfplumber first; falls back to pymupdf for corrupt/non-standard PDFs.

    Returns:
        dict  {voucher_str: income_tax_str}
    """
    all_text = ""
    try:
        all_text = _extract_text_pdfplumber(pdf_path)
        print("  PDF engine: pdfplumber")
    except Exception as e:
        if not PYMUPDF_AVAILABLE:
            sys.exit(
                f"Error reading PDF: {e}\n"
                "Try installing pymupdf as a fallback: pip install pymupdf"
            )
        print(f"  pdfplumber failed ({type(e).__name__}), retrying with pymupdf...")
        try:
            all_text = _extract_text_pymupdf(pdf_path)
            print("  PDF engine: pymupdf (fallback)")
        except Exception as e2:
            sys.exit(
                f"Both PDF engines failed.\n"
                f"  pdfplumber : {e}\n"
                f"  pymupdf    : {e2}\n"
                "The file may be corrupted, password-protected, or not a valid PDF."
            )

    # Groups: (1) voucher  (2) date  (3) gross  (4) income_tax
    # Some PDF rows render thousands separator as '.' instead of ','
    # (e.g. 999.988 or 13.932). Allow both and strip when cleaning —
    # all Form 93 amounts are integers, no real decimal points.
    pattern = re.compile(
        r"(\d{1,4})\s+"
        r"(\d{2}/\d{2}/202\d?)\s+"
        r"([\d,.]+)\s+"
        r"([\d,.]+)\s"
    )

    records = {}
    for m in pattern.finditer(all_text):
        voucher = m.group(1)
        it      = m.group(4).replace(",", "").replace(".", "")
        if int(voucher) > 0 and voucher not in records:
            records[voucher] = it

    return records


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------

def load_json_records(json_path: str) -> dict:
    """
    Load JSON and return voucher -> {it, name}.

    Supports:
      {"data": [{"voucher": "2", "incomeTax": "18068", "nameOfAgency": "..."}, ...]}
      or a plain list at the root.

    Returns:
        dict  {voucher_str: {"it": str, "name": str}}
    """
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)

    rows = raw.get("data", raw) if isinstance(raw, dict) else raw

    records = {}
    for r in rows:
        voucher = str(r.get("voucher", "")).strip()
        it      = str(r.get("incomeTax", "")).strip()
        name    = str(r.get("nameOfAgency", r.get("name", ""))).strip()
        if voucher:
            records[voucher] = {"it": it, "name": name}

    return records


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(pdf_records: dict, json_records: dict) -> list:
    """
    Compare income tax values for every voucher present in JSON.
    Vouchers missing from PDF extraction are flagged as NOT_FOUND_IN_PDF.

    Returns list of dicts with keys:
        voucher, name, pdf_it, json_it, match, status
    """
    results = []

    for voucher in sorted(json_records.keys(), key=int):
        j      = json_records[voucher]
        pdf_it = pdf_records.get(voucher)

        if pdf_it is None:
            results.append({
                "voucher": voucher,
                "name":    j["name"],
                "pdf_it":  "NOT FOUND",
                "json_it": j["it"],
                "match":   False,
                "status":  "NOT_FOUND_IN_PDF",
            })
        elif pdf_it == j["it"]:
            results.append({
                "voucher": voucher,
                "name":    j["name"],
                "pdf_it":  pdf_it,
                "json_it": j["it"],
                "match":   True,
                "status":  "MATCH",
            })
        else:
            results.append({
                "voucher": voucher,
                "name":    j["name"],
                "pdf_it":  pdf_it,
                "json_it": j["it"],
                "match":   False,
                "status":  "MISMATCH",
            })

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(results: list, pdf_path: str, json_path: str):
    total      = len(results)
    matched    = sum(1 for r in results if r["status"] == "MATCH")
    mismatched = sum(1 for r in results if r["status"] == "MISMATCH")
    not_found  = sum(1 for r in results if r["status"] == "NOT_FOUND_IN_PDF")

    bar = "=" * 60
    print(f"\n{bar}")
    print("  FORM 93  -  INCOME TAX VALIDATION REPORT")
    print(bar)
    print(f"  PDF  : {pdf_path}")
    print(f"  JSON : {json_path}")
    print(bar)
    print(f"  Total JSON records        : {total}")
    print(f"  IT matched                : {matched}")
    print(f"  IT mismatch               : {mismatched}")
    print(f"  Not found in PDF          : {not_found}")
    print(bar)

    if mismatched > 0:
        print("\n  INCOME TAX MISMATCHES")
        print(f"  {'Voucher':<10} {'Contractor':<38} {'PDF IT':>10} {'JSON IT':>10}")
        print(f"  {'-'*10} {'-'*38} {'-'*10} {'-'*10}")
        for r in results:
            if r["status"] == "MISMATCH":
                print(f"  {r['voucher']:<10} {r['name'][:38]:<38} {r['pdf_it']:>10} {r['json_it']:>10}")
        print()

    if not_found > 0:
        vouchers = [r["voucher"] for r in results if r["status"] == "NOT_FOUND_IN_PDF"]
        print(f"\n  Vouchers not extracted from PDF: {', '.join(vouchers)}")
        print("  (These rows may have text-wrapping issues in the PDF source.)")

    print(f"\n{bar}\n")


def write_csv(results: list, out_path: str):
    fieldnames = ["voucher", "name", "pdf_it", "json_it", "match", "status"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"  CSV saved -> {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def find_files_in_folder(folder: str):
    """Auto-discover the first PDF and JSON inside a folder."""
    folder_path = Path(folder)
    pdfs  = list(folder_path.glob("*.pdf"))
    jsons = list(folder_path.glob("*.json"))

    if not pdfs:
        sys.exit(f"Error: No PDF file found in folder '{folder}'")
    if not jsons:
        sys.exit(f"Error: No JSON file found in folder '{folder}'")
    if len(pdfs) > 1:
        print(f"  Warning: Multiple PDFs found, using '{pdfs[0].name}'")
    if len(jsons) > 1:
        print(f"  Warning: Multiple JSONs found, using '{jsons[0].name}'")

    return str(pdfs[0]), str(jsons[0])


def main():
    parser = argparse.ArgumentParser(
        description="Validate Form 93 income tax values (PDF vs JSON).",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python form93_validate.py data\\\n"
            "  python form93_validate.py --pdf form_93.pdf --json data.json\n"
            "  python form93_validate.py --pdf form_93.pdf --json data.json --out results.csv\n"
        )
    )
    parser.add_argument("folder", nargs="?",    help="Folder containing one PDF and one JSON (auto mode)")
    parser.add_argument("--pdf",  default=None, help="Path to the Form 93 PDF file")
    parser.add_argument("--json", default=None, help="Path to the JSON data file")
    parser.add_argument("--out",  default=None, help="Optional: path to save CSV report")
    args = parser.parse_args()

    if args.folder:
        pdf_path, json_path = find_files_in_folder(args.folder)
    elif args.pdf and args.json:
        pdf_path  = args.pdf
        json_path = args.json
    else:
        parser.print_help()
        sys.exit("\nError: Provide either a folder path OR both --pdf and --json.")

    if not Path(pdf_path).exists():
        sys.exit(f"Error: PDF not found at '{pdf_path}'")
    if not Path(json_path).exists():
        sys.exit(f"Error: JSON not found at '{json_path}'")
    _check_pdf_file(pdf_path)

    print(f"\nExtracting income tax from PDF  -> {pdf_path}")
    pdf_records = extract_pdf_records(pdf_path)
    print(f"  Found {len(pdf_records)} voucher records in PDF")

    print(f"Loading income tax from JSON    -> {json_path}")
    json_records = load_json_records(json_path)
    print(f"  Found {len(json_records)} voucher records in JSON")

    print("\nRunning income tax validation...")
    results = validate(pdf_records, json_records)

    print_summary(results, pdf_path, json_path)

    out_path = args.out or str(Path(pdf_path).with_suffix("")) + "_it_validation.csv"
    write_csv(results, out_path)


if __name__ == "__main__":
    main()