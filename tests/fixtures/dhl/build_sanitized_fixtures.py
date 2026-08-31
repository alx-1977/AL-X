"""Build public DHL fixtures from non-sensitive facts measured in private V1 PDFs.

The source documents remain in the private JARVIS repository. These fixtures
contain only the identifiers and totals already recorded in AL/X's public
implementation evidence. Coordinates preserve the relative Crystal Reports
text runs used by the parser, including the VAT value appearing beside the
neighbouring TOTAL 12B label rather than directly beside TOTAL VAT.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


ROOT = Path(__file__).resolve().parent
PAGE_SIZE = (792, 612)


CASES = (
    {
        "name": "dfm_20260421",
        "declaration": "DFM202604215028901",
        "waybill": "8339567983",
        "duty": "15.60",
        "vat": "1,100.55",
        "total": "1,116.15",
        "total_y": 208.2,
    },
    {
        "name": "dfm_20260719",
        "declaration": "DFM202607195025382",
        "waybill": "7096903730",
        "duty": "38.25",
        "vat": "168.75",
        "total": "207.00",
        "total_y": 244.2,
    },
)


def _write(path: Path, title: str, runs: list[tuple[float, float, str]]) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=PAGE_SIZE[0], height=PAGE_SIZE[1])
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    commands = ["BT"]
    for index, (x, y, text) in enumerate(runs):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        # Alternating imperceptibly different sizes preserves separate text
        # runs in pypdf, as Crystal Reports does with its own font changes.
        size = "8" if index % 2 == 0 else "8.001"
        commands.append(f"/F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj")
    commands.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata(
        {
            "/Title": title,
            "/Author": "AL/X test fixture generator",
            "/Creator": "AL/X test fixture generator",
            "/Subject": "Sanitized DHL parser regression fixture",
        }
    )
    with path.open("wb") as output:
        writer.write(output)


def worksheet(case: dict[str, str | float]) -> None:
    total_y = float(case["total_y"])
    _write(
        ROOT / f"worksheet_{case['name']}_sanitized.pdf",
        f"Sanitized customs worksheet {case['name']}",
        [
            (24.0, 541.0, "CUSTOMS WORKSHEET"),
            (84.0, 508.3, str(case["declaration"])),
            (399.5, 454.2, f"17. Export Country : 18. S.O.B. Date :{case['waybill']}"),
            (344.0, total_y, "37.Totals :"),
            (438.0, total_y + 1.0, "ZAR"),
            (458.0, total_y + 1.0, str(case["total"])),
            (344.0, total_y - 24.0, "TOTAL VAT"),
            (438.0, total_y - 23.0, "ZAR"),
            # Crystal Reports places this value on a neighbouring label's run.
            (552.5, total_y - 22.0, f"TOTAL 12B/Ad Val. {case['vat']}"),
            (344.0, total_y - 36.0, "TOTAL DUTY"),
            (438.0, total_y - 36.0, "ZAR"),
            (458.0, total_y - 35.0, str(case["duty"])),
        ],
    )


def sad500(case: dict[str, str | float]) -> None:
    _write(
        ROOT / f"sad500_{case['name']}_sanitized.pdf",
        f"Sanitized SAD 500 {case['name']}",
        [
            (56.4, 550.5, "SAD 500 - CUSTOMS DECLARATION FORM"),
            (430.6, 65.4, str(case["declaration"])),
        ],
    )


if __name__ == "__main__":
    for item in CASES:
        worksheet(item)
        sad500(item)
