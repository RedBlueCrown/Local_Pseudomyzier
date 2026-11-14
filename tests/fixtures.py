"""Synthetic court decisions (fake names) used across tests."""

from docx_factory import make_docx


def decision_vgh(**kw) -> bytes:
    body = [
        "VGH 2026/21",
        [("U R T E I L", {"b": True})],
        "Der Verwaltungsgerichtshof des Fürstentums Liechtenstein hat durch seine Richter Präsident Dr. Hugo Vogt, "
        "lic. iur. Thomas Hasler und Dr. Nicole Beck sowie im Beisein der Schriftführerin Marion Ospelt in der "
        "Beschwerdesache",
        ["Beschwerdeführer:\t", ("Albert Kaiser", {"b": True}), ", Dorfstrasse 5, 9490 Vaduz"],
        "vertreten durch:\tBerger & Partner Rechtsanwälte, Bergstrasse 1, 9490 Vaduz",
        "gegen",
        {"table": [["Beschwerdegegnerin:", {"paras": ["Papageien Anstalt", "Landstrasse 12", "9495 Triesen"]}]]},
        "wegen: Baubewilligung (Beschwerde gegen die Entscheidung der Regierung vom 3. März 2026, RA 2025/81)",
        "I. Sachverhalt",
        ["1. Albert Kaiser, geboren am 10.10.1990, ist Eigentümer des Vaduzer Grundstücks Nr. 4716. "
         "Die Papageien Anstalt beantragte die Überbauung der Parz. 4716/4718.", {"footnote": 1}],
        "2. Kaisers Antrag wurde von der Regierung abgewiesen. Der Hausarzt Dr. med. Rudolf Nova bestätigte dies; "
        "Dr. Nova ist Facharzt in Buchs. Kaiser erreicht man unter Tel. +423 232 12 34 oder a.kaiser@example.li.",
        "3. Die Muster Bank AG (FL-0002.123.456-7) hält das Konto IBAN LI21 0881 0000 2324 013A A. "
        "Das Fahrzeug mit dem Kontrollschild FL 12345 gehört Kaiser. Im Gebiet „Riet“ liegt Grundstück Nr. 4718.",
        "4. Gemäss Art. 44 Abs. 3 USG und LGBl. 1999/159 sowie StGH 2024/024 und VBK 2025/81; vgl. Kley/Vogt, "
        "Grundrechtspraxis, Rz 12; EGMR Kaiser v. Liechtenstein. Präsident Vogt hält fest: Vorschlag von Vogt.",
        "II. Begründung",
        "5. Die Beschwerde ist unbegründet. Der Verwaltungsgerichtshof folgt der Regierung.",
        "Vaduz, 10. Juni 2026",
        "Für den Verwaltungsgerichtshof",
        "Der Präsident",
        "Dr. Hugo Vogt",
    ]
    return make_docx(
        body=body,
        header=["VGH 2026/21 – Kaiser gegen Regierung"],
        footer=["Seite ", ],
        footnotes={1: ["Die Parzelle Nr. 4716 grenzt an das Grundstück von Albert Kaiser."]},
        custom={"Sachbearbeiter": "Hans Muster"},
        people=["Hans Muster"],
        **kw,
    )
